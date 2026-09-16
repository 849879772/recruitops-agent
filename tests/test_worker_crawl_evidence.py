from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import io
import json
import subprocess
import sys
from types import MappingProxyType, ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

from packages.pipeline import isolation
from packages.recruitment_core import job_details, worker
from packages.recruitment_core.models import CompanyConfig


@pytest.fixture
def entry_crawl(monkeypatch: pytest.MonkeyPatch) -> Mock:
    entry = Mock()
    module = ModuleType("packages.recruitment_core.entry_crawl")
    module.crawl_company_with_entry_discovery = entry
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        worker, "crawl_company", Mock(side_effect=AssertionError("legacy crawler called")),
    )
    return entry


def _request(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


@pytest.mark.parametrize("pagination_complete", [True, False, None])
def test_worker_evidence_serializes_mapping_without_dropping_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entry_crawl: Mock, pagination_complete: bool | None,
) -> None:
    result = {
        "jobs": [{"id": "partial-job", "title": "\u6821\u62db\u5de5\u7a0b\u5e08"}],
        "raw_job_count": 3, "pagination_complete": pagination_complete,
        "completeness_known": pagination_complete is not None,
        "pages_seen": 0, "total_pages": None, "has_more": False,
        "advertised_total": None, "termination_reasons": ["timeout"],
        "source_runs": [{"pagination_complete": None, "has_more": False}],
        "failures": ["timeout"], "effective_source_urls": ["https://example.test/campus"],
        "source_url": "https://example.test/jobs",
        "discovered_entry_url": "https://example.test/campus", "error_code": "partial_crawl",
        "ok": False, "extra_evidence": {"known": False, "value": None},
    }

    def crawl(company: CompanyConfig) -> Any:
        assert company.name == "Company"
        assert company.extra["source_cohort"] == 2027
        print("entry crawler diagnostic")
        return MappingProxyType(result)

    entry_crawl.side_effect = crawl
    _request(monkeypatch, {
        "operation": "crawl_company_evidence",
        "company": {"name": "Company", "crawler": "fixture", "source_cohort": 2027},
    })

    assert worker.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "result": result}
    assert len(captured.out.splitlines()) == 1
    assert captured.err == "entry crawler diagnostic\n"
    entry_crawl.assert_called_once()


@pytest.mark.parametrize("result", [None, [], "invalid", {}, {"jobs": None}, {"jobs": {}}])
def test_worker_rejects_malformed_crawl_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entry_crawl: Mock, result: Any,
) -> None:
    entry_crawl.return_value = result
    _request(monkeypatch, {"operation": "crawl_company_evidence", "company": {}})

    assert worker.main() == 1
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is False
    assert response["error_type"] == "ValueError"
    assert "crawler evidence result" in response["error"]


@pytest.mark.parametrize("payload", [
    [], None, {"operation": "crawl_company_evidence"},
    {"operation": "crawl_company_evidence", "company": []},
    {"operation": "unsupported"},
])
def test_worker_rejects_malformed_request_without_crawling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entry_crawl: Mock, payload: Any,
) -> None:
    _request(monkeypatch, payload)

    assert worker.main() == 1
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is False
    assert response["error_type"] == "ValueError"
    entry_crawl.assert_not_called()


def test_worker_reports_entry_exception_without_stdout_contamination(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entry_crawl: Mock,
) -> None:
    def fail(_company: CompanyConfig) -> Any:
        print("before entry failure")
        raise RuntimeError("entry discovery failed")

    entry_crawl.side_effect = fail
    _request(monkeypatch, {"operation": "crawl_company_evidence", "company": {}})

    assert worker.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False, "error_type": "RuntimeError", "error": "entry discovery failed",
    }
    assert captured.err == "before entry failure\n"


@pytest.mark.parametrize("operation", [None, "crawl_company"])
def test_worker_keeps_legacy_crawl_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entry_crawl: Mock, operation: str | None,
) -> None:
    jobs = [{"id": "legacy-job"}]
    legacy = Mock(return_value=jobs)
    monkeypatch.setattr(worker, "crawl_company", legacy)
    request: dict[str, Any] = {"company": {"name": "Company"}}
    if operation is not None:
        request["operation"] = operation
    _request(monkeypatch, request)

    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "jobs": jobs}
    legacy.assert_called_once()
    entry_crawl.assert_not_called()


def test_worker_keeps_structured_job_detail_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entry_crawl: Mock,
) -> None:
    hydration = job_details.JobDetailHydrationResult(
        detail="", status="api_variant_unsupported", source="fixture",
        detail_url="https://example.test/job", attempts=("fixture:unsupported",),
        error_type="ValueError",
    )
    fetch = Mock(return_value=hydration)
    monkeypatch.setattr(job_details, "fetch_full_job_description_result", fetch)
    job = {"id": "job-1", "detail_url": "https://example.test/job"}
    _request(monkeypatch, {"operation": "job_detail", "job": job})

    assert worker.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "detail": "", "hydration": json.loads(json.dumps(asdict(hydration))),
    }
    fetch.assert_called_once_with(job)
    entry_crawl.assert_not_called()


@pytest.fixture
def stubbed_worker_subprocess(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    real_popen = subprocess.Popen

    def install(result: Any) -> None:
        # Run the real worker and its pipe protocol, replacing only the entry
        # crawler inside the child. No fixture module or live network is needed.
        script = f"""
import runpy
import socket
import sys
from types import ModuleType

def deny_network(*args, **kwargs):
    raise AssertionError("network access is forbidden in the worker fixture")

socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

def crawl(company):
    assert company.name == "Fixture Company"
    print("child entry diagnostic")
    return {result!r}

entry = ModuleType("packages.recruitment_core.entry_crawl")
entry.crawl_company_with_entry_discovery = crawl
sys.modules[entry.__name__] = entry
runpy.run_module("packages.recruitment_core.worker", run_name="__main__")
"""

        def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
            assert command == [sys.executable, "-m", isolation.WORKER_MODULE]
            return real_popen([command[0], "-c", script], **kwargs)

        monkeypatch.setattr(isolation.subprocess, "Popen", popen)

    return install


@pytest.mark.parametrize("pagination_complete", [True, False, None])
def test_evidence_round_trips_through_actual_worker_subprocess(
    stubbed_worker_subprocess: Callable[[Any], None], pagination_complete: bool | None,
) -> None:
    evidence = {
        "jobs": [{"id": "partial-job", "title": "\u6821\u62db\u5de5\u7a0b\u5e08"}],
        "raw_job_count": 4, "pagination_complete": pagination_complete,
        "completeness_known": pagination_complete is not None,
        "pages_seen": 1, "total_pages": None, "has_more": False,
        "advertised_total": None, "termination_reasons": ["page_timeout"],
        "source_runs": [{"pagination_complete": None, "has_more": False}],
        "failures": ["page_timeout"], "effective_source_urls": ["https://example.test/campus"],
        "source_url": "https://example.test/jobs",
        "discovered_entry_url": "https://example.test/campus", "error_code": "partial_crawl",
    }
    stubbed_worker_subprocess(evidence)

    result = isolation.crawl_company_result_isolated(
        {"name": "Fixture Company", "crawler": "fixture"},
        timeout_seconds=20, python_executable=sys.executable,
    )

    assert result == evidence
    assert result["pagination_complete"] is pagination_complete


def test_actual_worker_subprocess_reports_invalid_entry_result(
    stubbed_worker_subprocess: Callable[[Any], None],
) -> None:
    stubbed_worker_subprocess({"jobs": None})

    with pytest.raises(isolation.IsolatedWorkerError, match="jobs list") as caught:
        isolation.crawl_company_result_isolated(
            {"name": "Fixture Company"}, timeout_seconds=20,
        )

    assert caught.value.error_type == "ValueError"
