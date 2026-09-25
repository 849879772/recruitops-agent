"""Browser lifetime checks for consecutive company listing renders."""

import os
from types import SimpleNamespace

import pytest
import requests

from packages.recruitment_core import resources, worker
from packages.recruitment_core.crawlers import render


class _Page:
    url = "https://jobs.example.test/list"

    def route(self, *_args):
        pass

    def goto(self, *_args, **_kwargs):
        pass

    def content(self):
        return "<main><article>招聘岗位</article></main>"


class _Context:
    def __init__(self, tracker):
        self.tracker = tracker

    def add_init_script(self, *_args):
        pass

    def new_page(self):
        return _Page()

    def close(self):
        self.tracker["contexts_closed"] += 1


class _Browser:
    def __init__(self, tracker):
        self.tracker = tracker

    def new_context(self, **_kwargs):
        self.tracker["contexts_created"] += 1
        return _Context(self.tracker)

    def close(self):
        self.tracker["browsers_closed"] += 1
        if self.tracker.get("close_raises"):
            raise RuntimeError("browser transport closed")


def _mock_browser(monkeypatch, tracker):
    class PlaywrightContext:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: launch()))

        def __exit__(self, *_args):
            tracker["playwright_closed"] += 1

    def launch():
        tracker["browsers_created"] += 1
        return _Browser(tracker)

    monkeypatch.setattr("playwright.sync_api.sync_playwright", PlaywrightContext)
    monkeypatch.setattr(render, "launch_browser", resources.launch_limited_browser)


def test_consecutive_hotjob_pages_reuse_browser_with_isolated_contexts(tmp_path, monkeypatch):
    tracker = dict(browsers_created=0, browsers_closed=0, contexts_created=0,
                   contexts_closed=0, playwright_closed=0)
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, "1")
    _mock_browser(monkeypatch, tracker)

    with worker._company_render_reuse(SimpleNamespace(crawler="hotjob")):
        assert render.render_page("https://jobs.example.test/list?page=1")
        assert render.render_page("https://jobs.example.test/list?page=2")
        assert tracker["browsers_created"] == 1
        assert tracker["contexts_created"] == tracker["contexts_closed"] == 2
        with pytest.raises(resources.ResourceLimitTimeout):
            resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.01)

    assert tracker["browsers_closed"] == tracker["playwright_closed"] == 1
    with resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.05):
        pass


def test_http_switch_and_exception_release_browser_lease(tmp_path, monkeypatch):
    tracker = dict(browsers_created=0, browsers_closed=0, contexts_created=0,
                   contexts_closed=0, playwright_closed=0)
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, "1")
    _mock_browser(monkeypatch, tracker)
    sent = []

    def fake_send(*_args, **_kwargs):
        sent.append(tracker["browsers_closed"])
        return None

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

    with pytest.raises(RuntimeError, match="fixture failure"):
        with worker._company_render_reuse(SimpleNamespace(crawler="ourpalm")):
            assert render.render_page("https://jobs.example.test/list?page=1")
            requests.adapters.HTTPAdapter().send(None)
            assert sent == [1]
            with resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.05):
                pass
            assert render.render_page("https://jobs.example.test/list?page=2")
            raise RuntimeError("fixture failure")

    assert tracker["browsers_created"] == tracker["browsers_closed"] == 2
    assert tracker["contexts_created"] == tracker["contexts_closed"] == 2
    with resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.05):
        pass


def test_hotjob_list_only_flag_is_scoped_to_evidence_operation(monkeypatch):
    key = "RECRUITOPS_HOTJOB_LIST_ONLY"
    monkeypatch.delenv(key, raising=False)
    hotjob = SimpleNamespace(crawler="hotjob")
    with worker._hotjob_list_only("crawl_company_evidence", hotjob):
        assert os.environ[key] == "1"
    assert key not in os.environ
    with worker._hotjob_list_only("crawl_company", hotjob):
        assert key not in os.environ
    monkeypatch.setenv(key, "original")
    with pytest.raises(RuntimeError):
        with worker._hotjob_list_only("crawl_company_evidence", hotjob):
            assert os.environ[key] == "1"
            raise RuntimeError("fixture")
    assert os.environ[key] == "original"


def test_moka_does_not_retain_a_second_render_browser(tmp_path, monkeypatch):
    tracker = dict(browsers_created=0, browsers_closed=0, contexts_created=0,
                   contexts_closed=0, playwright_closed=0)
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, "1")
    _mock_browser(monkeypatch, tracker)

    with worker._company_render_reuse(SimpleNamespace(crawler="moka")):
        assert render.render_page("https://jobs.example.test/list?page=1")
        with resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.05):
            pass
        assert render.render_page("https://jobs.example.test/list?page=2")

    assert tracker["browsers_created"] == tracker["browsers_closed"] == 2


def test_http_fallback_survives_browser_close_error_and_releases_slot(tmp_path, monkeypatch):
    tracker = dict(browsers_created=0, browsers_closed=0, contexts_created=0,
                   contexts_closed=0, playwright_closed=0, close_raises=True)
    monkeypatch.setenv(resources.RESOURCE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(resources.BROWSER_LIMIT_ENV, "1")
    _mock_browser(monkeypatch, tracker)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda *_args, **_kwargs: "http-result")

    with worker._company_render_reuse(SimpleNamespace(crawler="hotjob")):
        assert render.render_page("https://jobs.example.test/list")
        assert requests.adapters.HTTPAdapter().send(None) == "http-result"
        with resources.ResourcePool(tmp_path).acquire("browser", 1, timeout=0.05):
            pass
