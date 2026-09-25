from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from packages.pipeline import isolation


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: bool = False,
    ) -> None:
        self.pid = 4321
        self.stdin = object()
        self.stdout = object()
        self.stderr = object()
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self.terminated = False
        self.request = ""
        self.communicate_timeout = 0.0

    def communicate(self, _request: str, timeout: float) -> tuple[str, str]:
        self.request = _request
        self.communicate_timeout = timeout
        if self._timeout:
            raise subprocess.TimeoutExpired("worker", timeout)
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return None if self._timeout and not self.terminated else self.returncode

    def wait(self, timeout: float) -> int:
        self.terminated = True
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def test_isolated_crawler_returns_worker_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(
        stdout=json.dumps({"ok": True, "jobs": [{"id": "job-1"}]})
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    jobs = isolation.crawl_company_isolated(
        {"id": "co", "name": "Company", "crawler": "render"},
        timeout_seconds=30,
    )

    assert jobs == [{"id": "job-1"}]
    assert isinstance(jobs, list)
    assert json.loads(process.request)["operation"] == "crawl_company"


def test_isolated_crawl_passes_a_smaller_budget_to_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(stdout=json.dumps({"ok": True, "jobs": []}))
    launches: list[dict[str, Any]] = []

    def popen(*_args: Any, **kwargs: Any) -> _FakeProcess:
        launches.append(kwargs)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", popen)

    isolation.crawl_company_isolated(
        {"id": "co", "name": "Company", "crawler": "render"},
        timeout_seconds=60,
    )

    child_budget = float(launches[0]["env"][isolation.CRAWL_TIMEOUT_ENV])
    assert 0 < child_budget < 60
    assert process.communicate_timeout == 120
    assert float(launches[0]["env"]["RECRUITOPS_CRAWL_RESOURCE_DEADLINE"]) > isolation.time.monotonic()


def test_browser_limit_defaults_to_six_and_explicit_four_six_reach_worker(monkeypatch):
    process = _FakeProcess(stdout=json.dumps({"ok": True, "jobs": []}))
    environments = []

    def popen(*_args, **kwargs):
        environments.append(kwargs["env"])
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", popen)
    isolation.crawl_company_isolated({}, timeout_seconds=30)
    assert environments[-1]["RECRUITOPS_BROWSER_MAX_CONCURRENCY"] == "6"
    for limit in (4, 6):
        isolation.crawl_company_isolated({}, timeout_seconds=30, browser_max_concurrency=limit)
        assert environments[-1]["RECRUITOPS_BROWSER_MAX_CONCURRENCY"] == str(limit)
    with pytest.raises(ValueError, match="between 1 and 6"):
        isolation.crawl_company_isolated({}, timeout_seconds=30, browser_max_concurrency=7)


@pytest.mark.parametrize("entrypoint", [
    isolation.crawl_company_isolated, isolation.crawl_company_result_isolated,
    isolation.fetch_job_detail_isolated, isolation.fetch_job_detail_result_isolated,
])
def test_worker_resource_config_is_explicit_and_does_not_mutate_parent(
    entrypoint, monkeypatch, tmp_path,
) -> None:
    process = _FakeProcess(stdout=json.dumps({"ok": True, "jobs": [], "result": {"jobs": []}, "detail": ""}))
    launches = []

    def popen(*_args, **kwargs):
        launches.append(kwargs)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", popen)
    monkeypatch.setenv("RECRUITOPS_BROWSER_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("RECRUITOPS_CRAWL_RESOURCE_ROOT", "parent-fixture-root")
    entrypoint({}, timeout_seconds=30, resource_root=tmp_path, browser_max_concurrency=1)
    child = launches[0]["env"]
    assert child["RECRUITOPS_CRAWL_RESOURCE_ROOT"] == str(tmp_path.resolve())
    assert child["RECRUITOPS_BROWSER_MAX_CONCURRENCY"] == "1"
    assert float(child["RECRUITOPS_CRAWL_RESOURCE_DEADLINE"]) > isolation.time.monotonic()
    assert isolation.os.environ["RECRUITOPS_CRAWL_RESOURCE_ROOT"] == "parent-fixture-root"
    assert isolation.os.environ["RECRUITOPS_BROWSER_MAX_CONCURRENCY"] == "2"


def test_isolated_crawler_kills_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(timeout=True)
    commands: list[list[str]] = []
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        isolation.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command),
    )

    with pytest.raises(isolation.IsolatedCrawlerTimeout, match="hard timeout"):
        isolation.crawl_company_isolated(
            {"id": "co", "name": "Company", "crawler": "render"},
            timeout_seconds=1,
        )

    assert process.terminated is True
    if isolation.os.name == "nt":
        assert commands == [["taskkill", "/PID", "4321", "/T", "/F"]]


def test_isolated_cleanup_bounds_taskkill_and_falls_back_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(timeout=True)
    taskkill_timeouts: list[float] = []
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    def fail_taskkill(_command: list[str], **kwargs: Any) -> None:
        taskkill_timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired("taskkill", kwargs["timeout"])

    monkeypatch.setattr(isolation.subprocess, "run", fail_taskkill)

    with pytest.raises(isolation.IsolatedCrawlerTimeout):
        isolation.crawl_company_isolated(
            {"id": "co", "name": "Company", "crawler": "render"},
            timeout_seconds=1,
        )

    if isolation.os.name == "nt":
        assert taskkill_timeouts == [10]
    assert process.terminated is True


def test_isolated_crawler_surfaces_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "ok": False,
                "error_type": "PermissionError",
                "error": "site rejected request",
                "resource_timing": {"browser_wait_seconds": 2.5},
            }
        ),
        returncode=1,
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedWorkerError, match="site rejected request") as caught:
        isolation.crawl_company_isolated(
            {"id": "co", "name": "Company", "crawler": "render"},
            timeout_seconds=30,
        )
    assert caught.value.error_type == "PermissionError"
    assert caught.value.resource_timing == {"browser_wait_seconds": 2.5}


def test_worker_resource_timing_reaches_crawl_result(monkeypatch):
    process = _FakeProcess(stdout=json.dumps({
        "ok": True, "result": {"jobs": []},
        "resource_timing": {"http_wait_seconds": 1.25, "browser_acquisitions": 1},
    }))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *_a, **_k: process)
    result = isolation.crawl_company_result_isolated({}, timeout_seconds=30)
    assert result["resource_timing"] == {"http_wait_seconds": 1.25, "browser_acquisitions": 1}


def test_request_is_serialized_before_worker_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started = False

    def popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        nonlocal started
        started = True
        return _FakeProcess()

    monkeypatch.setattr(isolation.subprocess, "Popen", popen)

    with pytest.raises(TypeError):
        isolation.crawl_company_isolated(
            {"id": "co", "bad": {object(): "not-json-key"}},
            timeout_seconds=30,
        )

    assert started is False


def test_isolated_job_detail_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(stdout=json.dumps({"ok": True, "detail": "Full JD"}))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    detail = isolation.fetch_job_detail_isolated(
        {"id": "job-1", "detail_url": "https://example.test/job-1"},
        timeout_seconds=30,
    )

    assert detail == "Full JD"


def test_isolated_job_detail_retains_structured_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        stdout=json.dumps(
            {
                "ok": True,
                "detail": "",
                "hydration": {
                    "detail": "",
                    "status": "api_variant_unsupported",
                    "source": "moka_official",
                    "detail_url": "https://example.test/job-1",
                    "attempts": ["moka_official:api_variant_unsupported"],
                    "error_type": "",
                },
            }
        )
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    result = isolation.fetch_job_detail_result_isolated(
        {"id": "job-1", "detail_url": "https://example.test/job-1"},
        timeout_seconds=30,
    )

    assert result["status"] == "api_variant_unsupported"
    assert result["source"] == "moka_official"
    assert result["attempts"] == ["moka_official:api_variant_unsupported"]


@pytest.mark.parametrize("pagination_complete", [True, False, None])
def test_isolated_crawler_retains_all_evidence_and_partial_rows(
    monkeypatch: pytest.MonkeyPatch, pagination_complete: bool | None,
) -> None:
    evidence = {
        "jobs": [{"id": "job-1", "jd_raw": "partial description"}],
        "raw_job_count": 7,
        "pagination_complete": pagination_complete,
        "completeness_known": pagination_complete is not None,
        "pages_seen": 0,
        "total_pages": None,
        "has_more": False,
        "advertised_total": None,
        "termination_reasons": ["page_fetch_failed"],
        "source_runs": [{
            "source_url": "https://example.test/jobs",
            "pagination_complete": None,
            "pages_seen": None,
            "has_more": None,
        }],
        "failures": ["page_fetch_failed", {"error_code": "timeout", "retryable": False}],
        "effective_source_urls": ["https://example.test/campus"],
        "source_url": "https://example.test/jobs",
        "discovered_entry_url": "https://example.test/campus",
        "error_code": "partial_crawl",
        "extra_evidence": {"verified": False, "total": None},
        "ok": False,
    }
    process = _FakeProcess(stdout=json.dumps({"ok": True, "result": evidence}))
    launches: list[tuple[list[str], dict[str, Any]]] = []

    def popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        launches.append((command, kwargs))
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", popen)
    company = {"id": "co", "name": "Company", "crawler": "render"}
    result = isolation.crawl_company_result_isolated(
        company, timeout_seconds=30, python_executable="fixture-python",
    )

    assert result == evidence
    assert result["pagination_complete"] is pagination_complete
    assert json.loads(process.request) == {
        "operation": "crawl_company_evidence", "company": company,
    }
    assert launches[0][0] == ["fixture-python", "-m", isolation.WORKER_MODULE]
    assert launches[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("jobs", [[], [{"id": "partial-job"}]])
def test_isolated_crawler_does_not_infer_missing_evidence(
    monkeypatch: pytest.MonkeyPatch, jobs: list[dict[str, Any]],
) -> None:
    evidence = {"jobs": jobs}
    process = _FakeProcess(stdout=json.dumps({"ok": True, "result": evidence}))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    assert isolation.crawl_company_result_isolated({}, timeout_seconds=30) == evidence


def test_isolated_crawler_retains_explicit_empty_and_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = {
        "jobs": [], "raw_job_count": 0, "pagination_complete": None,
        "completeness_known": None, "pages_seen": None, "total_pages": 0,
        "has_more": None, "advertised_total": 0, "termination_reasons": [],
        "source_runs": [], "failures": [], "effective_source_urls": [],
        "source_url": "", "discovered_entry_url": None, "error_code": "",
    }
    process = _FakeProcess(stdout=json.dumps({"ok": True, "result": evidence}))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    assert isolation.crawl_company_result_isolated({}, timeout_seconds=30) == evidence


@pytest.mark.parametrize("payload", [
    {"ok": True, "jobs": []},
    {"ok": True, "result": None},
    {"ok": True, "result": []},
    {"ok": True, "result": "not an object"},
])
def test_isolated_crawler_rejects_missing_or_malformed_result(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
) -> None:
    process = _FakeProcess(stdout=json.dumps(payload))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedOperationError, match="result object"):
        isolation.crawl_company_result_isolated({}, timeout_seconds=30)


@pytest.mark.parametrize("result", [{}, {"jobs": None}, {"jobs": {}}, {"jobs": [None]}])
def test_isolated_crawler_rejects_malformed_evidence_jobs(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any],
) -> None:
    process = _FakeProcess(stdout=json.dumps({"ok": True, "result": result}))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedOperationError, match="jobs list"):
        isolation.crawl_company_result_isolated({}, timeout_seconds=30)


@pytest.mark.parametrize(("field", "value"), [
    ("pagination_complete", "false"), ("pagination_complete", 0),
    ("pagination_complete", 1), ("completeness_known", "unknown"),
    ("has_more", []), ("raw_job_count", True), ("raw_job_count", -1),
    ("pages_seen", "2"), ("total_pages", 1.5), ("advertised_total", -1),
    ("termination_reasons", "timeout"), ("termination_reasons", [None]),
    ("source_runs", {}), ("source_runs", [False]),
    ("source_runs", [{"pagination_complete": "true"}]),
    ("source_runs", [{"pages_seen": False}]),
    ("failures", "timeout"), ("failures", [True]),
    ("effective_source_urls", [None]), ("source_url", []),
    ("discovered_entry_url", False), ("error_code", {}),
])
def test_isolated_crawler_rejects_malformed_evidence_fields(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any,
) -> None:
    process = _FakeProcess(stdout=json.dumps({
        "ok": True, "result": {"jobs": [{"id": "job-1"}], field: value},
    }))
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedOperationError, match=field):
        isolation.crawl_company_result_isolated({}, timeout_seconds=30)


@pytest.mark.parametrize(("stdout", "message"), [
    ("not-json", "invalid JSON"), ("[]", "JSON object"), ("null", "JSON object"),
])
def test_isolated_crawler_evidence_rejects_malformed_wire_response(
    monkeypatch: pytest.MonkeyPatch, stdout: str, message: str,
) -> None:
    process = _FakeProcess(stdout=stdout)
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedOperationError, match=message):
        isolation.crawl_company_result_isolated({}, timeout_seconds=30)


@pytest.mark.parametrize("returncode", [0, 1])
def test_isolated_crawler_evidence_surfaces_typed_worker_errors(
    monkeypatch: pytest.MonkeyPatch, returncode: int,
) -> None:
    process = _FakeProcess(
        stdout=json.dumps({"ok": False, "error_type": "ValueError", "error": "bad result"}),
        returncode=returncode,
    )
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(isolation.IsolatedWorkerError, match="bad result") as caught:
        isolation.crawl_company_result_isolated({}, timeout_seconds=30)

    assert caught.value.error_type == "ValueError"


def test_isolated_crawler_evidence_cleans_up_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(timeout=True)
    cleaned: list[_FakeProcess] = []
    monkeypatch.setattr(isolation.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(isolation, "_terminate_process_tree", cleaned.append)

    with pytest.raises(isolation.IsolatedCrawlerTimeout, match="hard timeout"):
        isolation.crawl_company_result_isolated({}, timeout_seconds=1)

    assert cleaned == [process]
