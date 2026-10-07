"""Offline regressions for status wording and complete final-result reporting."""

from time import perf_counter

import pytest

from packages.domain.application_status_semantics import noncanonical_status_reason
from packages.storage import ApplicationSnapshot
from packages.tools.application_review_run import _response
from packages.tools.application_review_summary import review_reason_breakdown
from packages.tools.application_status_evidence import (
    VerifyApplicationStatusEvidenceInput, verify_application_status_evidence, supports_no_newer_status,
)
from packages.tools.batch_browser_operations import ApplicationStatusResult
from packages.tools.browser_status_update import BrowserStatusUpdateInput, browser_status_update
from tests.test_application_status_model_fallback import _case, _card, URL


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer"])
def test_explicit_resume_routing_confirms_processing_without_stage_regression(tmp_path, monkeypatch, stage):
    card = _card("示例工程师", "分配简历-流程中")
    repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[card], stage=stage)
    result = run()
    assert result.unchanged[0].reason == "no_newer_status_observed"
    assert result.summary["reason_breakdown"]["unchanged"] == {"no_newer_status_observed": 1}
    assert not result.updated and not client.calls
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == stage


@pytest.mark.parametrize("label", [
    "分配简历-未通过", "分配简历-流程终止", "分配简历-已撤回", "流程中",
    "将分配简历-流程中", "可能分配简历-流程中", "尚未分配简历-流程中",
])
def test_routing_baseline_does_not_accept_future_negation_or_terminal(label):
    assert not supports_no_newer_status(_card("示例工程师", label))


@pytest.mark.parametrize("label,reason", [
    ("已归入公司人才库。", "talent_pool_status_unmapped"),
    ("推荐到其他职位", "position_recommendation_unmapped"),
])
@pytest.mark.parametrize("stage", ["applied", "interview1"])
def test_meaningful_unmapped_labels_keep_original_and_never_force_terminal(tmp_path, monkeypatch, label, reason, stage):
    card = _card("示例工程师", label)
    repository, store, client, run, ops = _case(tmp_path, monkeypatch, cards=[card], stage=stage)
    result = run()
    assert result.unresolved[0].reason == reason
    assert result.unresolved[0].observed_label == label
    assert result.summary["reason_breakdown"]["unresolved"] == {reason: 1}
    assert not result.updated and not result.unchanged and not client.calls
    for forced_stage in ["rejected", "offer"]:
        verified = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
            application_id="24", observation_operation_id=ops[0], observed_status=forced_stage,
            observed_label=label, evidence=card["context"], confidence=1,
            captured_at="2026-09-28T01:00:00Z"), store)
        assert verified.error_code == "status_semantics_unsupported"
        direct = browser_status_update(BrowserStatusUpdateInput(application_id="24", page_url=URL,
            terminal_result={"operation_status": "SUCCEEDED", "captured_at": "2026-09-28T01:00:00Z",
                "entries": [{**card, "status": forced_stage, "confidence": 1}]}), repository.storage)
        assert direct.data.reason_code == "status_semantics_unsupported"
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == stage


@pytest.mark.parametrize("label", [
    "如果未通过将进入人才库", "未归入人才库", "推荐其他职位按钮",
    "笔试未通过，已归入人才库", "已收到Offer，归入人才库",
])
def test_noncanonical_reasons_only_describe_asserted_current_labels(label):
    assert noncanonical_status_reason(label) is None


@pytest.mark.parametrize("label", ["初试", "复试"])
def test_explicit_current_interview_step_can_update(tmp_path, monkeypatch, label):
    repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[_card("示例工程师", label)])
    result = run()
    assert len(result.updated) == 1, result.model_dump()
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "interview1"


def test_checkpoint_summary_explains_all_77_unknowns_and_163_results():
    counts = {
        "unchanged": {"no_newer_status_observed": 70}, "excluded": {"mail_only": 9},
        "blocked": {"login_required": 5}, "failed": {"desktop_navigation_changed": 2},
        "unresolved": {"record_present_status_unknown": 48, "frame_scope_denied": 16,
            "unparsed_page": 7, "target_record_not_matched": 3, "model_uncertain": 3},
    }
    rows = []
    for state, reasons in counts.items():
        for reason, count in reasons.items():
            for _ in range(count):
                rows.append(ApplicationStatusResult(application_id=str(len(rows)), state=state, reason=reason, elapsed_ms=0))
    ids = [row.application_id for row in rows]
    state = {"ids": ids + ids[:1], "results": {row.application_id: row.model_dump() for row in rows},
        "database_total": 180, "excluded_terminal": 17, "pages_total": 150, "run_status": "completed"}
    state["results"]["outside"] = {**rows[0].model_dump(), "reason": "outside_scope"}
    result = _response("status-review-" + "0" * 32, state, perf_counter())
    summary = result.summary
    assert summary["total"] == summary["processed_count"] == 163
    assert summary["unresolved"] == 77
    breakdown = summary["reason_breakdown"]
    assert breakdown == review_reason_breakdown(rows)
    for bucket, reasons in breakdown.items():
        assert sum(reasons.values()) == summary[bucket]
    assert breakdown["failed"] == {"desktop_navigation_changed": 2}
    assert not any("timeout" in reason for reasons in breakdown.values() for reason in reasons)


def test_missing_reason_is_counted_and_empty_summary_is_complete():
    result = review_reason_breakdown([ApplicationStatusResult(application_id="fixture", state="failed", elapsed_ms=0)])
    assert result["failed"] == {"reason_not_reported": 1}
    assert all(not reasons for reasons in review_reason_breakdown([]).values())
