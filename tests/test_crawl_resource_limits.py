from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import gc
import json
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from packages.config import Settings
from packages.recruitment_core import resources


def _hold_process_slot(root, name, ready):
    with resources.ResourcePool(root).acquire(name, 2, timeout=2):
        ready.set()
        threading.Event().wait(20)


def test_concurrency_defaults_and_legacy_explicit_settings(monkeypatch):
    for field in ("CRAWL_MAX_CONCURRENCY", "DETAIL_MAX_CONCURRENCY", "MATCH_MAX_CONCURRENCY",
                  "BROWSER_MAX_CONCURRENCY", "WRITE_ENABLED"):
        monkeypatch.delenv("RECRUITOPS_" + field, raising=False)
    settings = Settings(_env_file=None)
    assert (settings.crawl_max_concurrency, settings.detail_max_concurrency,
            settings.match_max_concurrency, settings.browser_max_concurrency) == (10, 10, 6, 6)
    assert settings.write_enabled is False
    legacy = Settings(_env_file=None, crawl_max_concurrency=4, match_max_concurrency=4)
    assert (legacy.crawl_max_concurrency, legacy.match_max_concurrency,
            legacy.detail_max_concurrency) == (4, 4, 10)
    assert Settings(_env_file=None, browser_max_concurrency=6).browser_max_concurrency == 6
    with pytest.raises(ValueError):
        Settings(_env_file=None, browser_max_concurrency=7)


def test_ten_companies_with_internal_threads_share_two_http_slots(tmp_path, monkeypatch):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    guard = threading.Lock()
    active = peak = 0

    def transport(_adapter, request, **_kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.01)
            response = requests.Response()
            response.status_code = 200
            response._content = b"fixture"
            response.request = request
            return response
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)

    def company(_company):
        with ThreadPoolExecutor(max_workers=4) as internal:
            return list(internal.map(lambda _: requests.get("https://jobs.example/detail").text, range(4)))

    with resources.worker_http_limits():
        with ThreadPoolExecutor(max_workers=10) as outer:
            results = list(outer.map(company, range(10)))
    assert peak == 2
    assert results == [["fixture"] * 4] * 10
    assert requests.adapters.HTTPAdapter.send is transport


@pytest.mark.parametrize("name", ["browser", "http:jobs.example"])
def test_slots_are_shared_across_processes_and_returned_after_termination(tmp_path, name):
    context = multiprocessing.get_context("spawn")
    processes = []
    try:
        for _ in range(2):
            ready = context.Event()
            process = context.Process(target=_hold_process_slot, args=(str(tmp_path), name, ready))
            process.start()
            processes.append(process)
            assert ready.wait(10), "fixture worker did not acquire a slot"
        pool = resources.ResourcePool(tmp_path)
        with pytest.raises(resources.ResourceLimitTimeout):
            pool.acquire(name, 2, timeout=0.05)
        processes[0].terminate()
        processes[0].join(5)
        with pool.acquire(name, 2, timeout=1):
            with pytest.raises(resources.ResourceLimitTimeout):
                pool.acquire(name, 2, timeout=0.05)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)


def test_exception_and_cancellation_return_slots_without_hanging(tmp_path):
    pool = resources.ResourcePool(tmp_path)
    with pytest.raises(RuntimeError):
        with pool.acquire("one", 1):
            raise RuntimeError("fixture")
    with pool.acquire("one", 1, timeout=0.05):
        cancelled = threading.Event()
        cancelled.set()
        started = time.monotonic()
        with pytest.raises(resources.ResourceLimitTimeout, match="cancelled"):
            pool.acquire("one", 1, cancel=cancelled)
        assert time.monotonic() - started < 0.5


def test_admission_uses_bounded_resource_wait_budget(monkeypatch):
    monkeypatch.setattr(resources, "_wait_seconds", {"browser": 0.0, "http": 0.0})
    monkeypatch.delenv(resources.RESOURCE_DEADLINE_ENV, raising=False)
    assert resources.admission_timeout() == 60
    monkeypatch.setattr(resources.time, "monotonic", lambda: 100.0)
    monkeypatch.setenv(resources.RESOURCE_DEADLINE_ENV, "350")
    assert resources.admission_timeout() == 60
    monkeypatch.setenv(resources.RESOURCE_DEADLINE_ENV, "90")
    assert resources.admission_timeout() == 0


def test_consecutive_failures_share_cooldown_and_honor_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr(resources.time, "time", lambda: 100.0)
    pool = resources.ResourcePool(tmp_path)
    url = "https://jobs.example/detail"
    pool.record_http(url, failed=True)
    with pool._state("jobs.example") as state:
        assert state == {"failures": 1, "until": 100.5}
    pool.record_http(url, status=429)
    with pool._state("jobs.example") as state:
        assert state == {"failures": 2, "until": 101.0}
    pool.record_http(url, status=429, retry_after="120")
    pool.record_http(url, status=200)
    with pool._state("jobs.example") as state:
        assert state["until"] == 220.0
    with pytest.raises(resources.ResourceLimitTimeout, match="cooldown"):
        resources.ResourcePool(tmp_path).acquire_http(url, timeout=0.01)


def test_transport_exception_releases_http_slot_and_restores_patch(tmp_path, monkeypatch):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))

    def transport(*_args, **_kwargs):
        raise requests.Timeout("synthetic timeout")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)
    with resources.worker_http_limits():
        with pytest.raises(requests.Timeout, match="synthetic"):
            requests.get("https://jobs.example/detail")
    assert requests.adapters.HTTPAdapter.send is transport
    pool = resources.ResourcePool(tmp_path)
    with pool.acquire("http:jobs.example", 2, timeout=0.05):
        with pool.acquire("http:jobs.example", 2, timeout=0.05):
            pass
    with pool._state("jobs.example") as state:
        assert state["failures"] == 1


def test_http_redirect_releases_origin_lease_before_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    pool = resources.ResourcePool(tmp_path)
    calls = []

    def transport(_adapter, request, **_kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response._content = b"fixture"
        if request.url.endswith("/start"):
            response.status_code = 302
            response.headers["Location"] = "https://jobs.example/end"
        else:
            response.status_code = 200
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)
    # Only one free host slot remains. A redirect retaining the original slot
    # would deadlock instead of reaching the synthetic second response.
    with pool.acquire("http:jobs.example", 2):
        with resources.worker_http_limits():
            assert requests.get("https://jobs.example/start").status_code == 200
    assert calls == ["https://jobs.example/start", "https://jobs.example/end"]


def test_dropped_stream_response_returns_lease(tmp_path, monkeypatch):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))

    def transport(_adapter, request, **_kwargs):
        response = requests.Response()
        response.request = request
        response.status_code = 200
        response._content = b"fixture"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)
    pool = resources.ResourcePool(tmp_path)
    with pool.acquire("http:jobs.example", 2):
        with resources.worker_http_limits():
            response = requests.get("https://jobs.example/detail", stream=True)
            with pytest.raises(resources.ResourceLimitTimeout):
                pool.acquire("http:jobs.example", 2, timeout=0.01)
            del response
            gc.collect()
            with pool.acquire("http:jobs.example", 2, timeout=0.1):
                pass


def test_worker_resource_setup_failure_has_structured_error(monkeypatch, capsys):
    from packages.recruitment_core import worker

    def fail_setup():
        raise PermissionError("fixture resource root denied")

    monkeypatch.setattr(resources, "worker_http_limits", fail_setup)
    assert worker.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error_type": "PermissionError", "error": "fixture resource root denied",
    }


def test_browser_launch_failure_close_and_disconnect_release_slots(tmp_path, monkeypatch):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, "1")
    pool = resources.ResourcePool(tmp_path)

    def failed_launch(**_kwargs):
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError, match="launch failed"):
        resources.launch_limited_browser(SimpleNamespace(chromium=SimpleNamespace(launch=failed_launch)))
    with pool.acquire("browser", 1, timeout=0.05):
        pass
    callbacks = {}

    def failed_close():
        raise RuntimeError("close failed")

    browser = SimpleNamespace(close=failed_close, on=lambda event, callback: callbacks.update({event: callback}))
    resources.launch_limited_browser(SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser)))
    with pytest.raises(resources.ResourceLimitTimeout):
        pool.acquire("browser", 1, timeout=0.01)
    callbacks["disconnected"]()
    with pool.acquire("browser", 1, timeout=0.05):
        pass
    with pytest.raises(RuntimeError, match="close failed"):
        browser.close()
    with pool.acquire("browser", 1, timeout=0.05):
        pass


@pytest.mark.parametrize("configured, expected", [(None, 6), ("4", 4), ("6", 6)])
def test_browser_pool_honors_configured_cap_and_records_wait(tmp_path, monkeypatch, configured, expected):
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    if configured is None:
        monkeypatch.delenv(resources.BROWSER_LIMIT_ENV, raising=False)
    else:
        monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, configured)
    before = resources.resource_timing_snapshot()["browser_acquisitions"]
    browsers = []
    try:
        for _ in range(expected):
            browser = SimpleNamespace(close=lambda: None)
            browsers.append(resources.launch_limited_browser(
                SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser))
            ))
        with pytest.raises(resources.ResourceLimitTimeout):
            resources.ResourcePool(tmp_path).acquire("browser", expected, timeout=0.02)
        assert resources.resource_timing_snapshot()["browser_acquisitions"] - before == expected
    finally:
        for browser in browsers:
            browser.close()
    with resources.ResourcePool(tmp_path).acquire("browser", expected, timeout=0.1):
        pass
