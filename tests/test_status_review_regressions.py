from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import asyncio

import pytest

from packages.mcp.server import TOOL_DEFINITIONS, _build_handler
from packages.tools.application_status_evidence import (
    VerifyApplicationStatusEvidenceInput, VerifyApplicationStatusEvidenceResponse,
    verify_application_status_evidence, has_unmapped_status,
)
from packages.tools.batch_browser_operations import BatchObserveApplicationStatusInput, batch_observe_application_status
from tests.test_model_driven_application_status import _prepared_store, _observed_operation
from tests.test_batch_browser_operations import _repository, _observed


def request_for(operation, *, status="applied", label="投递", evidence="投递", confidence=0.75):
    return VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id,
        observed_status=status, observed_label=label, evidence=evidence, confidence=confidence,
        captured_at="2026-08-22T12:00:00Z",
    )


@pytest.mark.parametrize("signals", [{"has_explicit_status": True}, {}])
def test_historical_unknown_status_is_not_absent(signals):
    assert has_unmapped_status({"status": "", "context": "状态: 暂不匹配", "signals": signals})


@pytest.mark.parametrize("entries", [[], [{"status": "rejected", "context": "另一岗位 流程终止"}]])
def test_batch_returns_unknown_text_for_model_instead_of_unchanged(tmp_path, monkeypatch, entries):
    from packages.tools import batch_browser_operations as module
    repository = _repository(tmp_path, [{"id": "24", "title": "软件开发工程师", "record_url": "https://ats.example/applications"}])
    async def observe(*args):
        return _observed({"entries": entries, "application_records": [{
            "title": "软件开发工程师", "status": "", "context": "软件开发工程师 状态: 暂不匹配",
            "signals": {"has_explicit_status": True},
        }]})
    monkeypatch.setattr(module, "observe_application_status_page_workflow", observe)
    result = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["24"]), object(), repository))
    assert not result.unchanged and not result.updated
    assert result.unresolved[0].reason == "status_unmapped"
    assert "暂不匹配" in str(result.unresolved[0].observation)


def test_mcp_verifier_returns_readonly_and_error_results(tmp_path):
    store, url = _prepared_store(tmp_path)
    operation = _observed_operation(store, url, observation={
        "page_url": url, "captured_at": "2026-08-22T12:00:00Z",
        "application_records": [{"title": "软件开发工程师", "context": "软件开发工程师 投递", "status": ""}],
    })
    definition = next(d for d in TOOL_DEFINITIONS if d.name == "verify_application_status_evidence")
    handler = _build_handler(definition, SimpleNamespace(browser_bridge=store))
    result = handler(request_for(operation))
    assert result.success and result.read_only
    error = handler(request_for(operation, evidence="not present"))
    assert error.error_code == "evidence_not_in_observation" and not error.retryable
    forbidden = replace(definition, read_only=True, operation=lambda *args: VerifyApplicationStatusEvidenceResponse(success=True, status="updated"))
    with pytest.raises(RuntimeError, match="read_only"):
        _build_handler(forbidden, None)(request_for(operation))


def test_unknown_label_can_be_interpreted_and_written_without_mail(tmp_path):
    store, url = _prepared_store(tmp_path)
    quote = "软件开发工程师 状态: 暂不匹配"
    operation = _observed_operation(store, url, observation={
        "page_url": url, "captured_at": "2026-08-22T12:00:00Z", "entries": [],
        "application_records": [{"title": "软件开发工程师", "status": "", "context": quote,
                                 "signals": {"has_explicit_status": True}}],
    })
    low = verify_application_status_evidence(request_for(operation, label="状态", evidence=quote), store)
    assert not low.success and low.reason_code == "confidence_below_threshold"
    result = verify_application_status_evidence(request_for(operation, status="rejected", label="暂不匹配", evidence=quote, confidence=0.99), store)
    assert result.success and result.status == "updated"
    assert result.verification.data.wrote


def test_unexpected_exception_has_structured_nonretryable_result():
    class BrokenStore:
        def get_operation(self, *args):
            raise RuntimeError("private diagnostic")
    result = verify_application_status_evidence(request_for(SimpleNamespace(operation_id="broken")), BrokenStore())
    assert result.error_code == "verification_internal_error"
    assert not result.retryable and "private diagnostic" not in result.error_message


def test_mail_status_hides_completed_history_by_default():
    from packages.recruitment_mail.processing import processing_status
    class Store:
        def query(self, **kwargs):
            return [SimpleNamespace(id=str(i), subject="old" if i else "pending", processing_status="processed" if i else "pending", processing_error=None, raw_metadata={}, content_digest="test") for i in range(2)]
    from unittest.mock import patch
    with patch("packages.recruitment_mail.processing.get_model_analysis", return_value=None):
        assert len(processing_status(Store())["items"]) == 1
        assert len(processing_status(Store(), include_history=True)["items"]) == 2


def test_dom_preserves_unknown_label_and_maps_explicit_rejection():
    from playwright.sync_api import sync_playwright
    from tests.test_extension_actions import launch_fixture_browser
    script = Path(__file__).parents[1] / "extension/src/application-records.js"
    with sync_playwright() as p:
        browser = launch_fixture_browser(p)
        try:
            page = browser.new_page()
            for label, status in [("暂不匹配", "rejected"), ("正在等待业务决策", ""),
                                  ("线上测评-进行中", "assessment"), ("笔试-进行中", "written"),
                                  ("线上测评-未通过", "rejected")]:
                page.set_content(f'<article data-recruitops-application><h3>软件开发工程师</h3><p>第 1 志愿</p><p>状态: {label}</p></article>')
                page.add_script_tag(path=str(script))
                result = page.evaluate("RecruitOpsApplicationRecords.extract(document)")
                card = result["records"][0]
                assert card["status"] == status
                assert card["label"] == label and label in card["raw_status_labels"]
                if not status:
                    assert card["evidence_source"] == "unmapped-status"
        finally:
            browser.close()


def test_browser_assessment_retains_applied_but_written_advances():
    from packages.tools.browser_status_update import _target_stage
    from packages.domain.models import ApplicationStage
    assert _target_stage(ApplicationStage.APPLIED, "assessment") == ApplicationStage.APPLIED
    assert _target_stage(ApplicationStage.APPLIED, "written") == ApplicationStage.WRITTEN
