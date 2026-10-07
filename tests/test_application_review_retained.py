"""Neutral review reporting is not authority to update or verify a saved stage."""

import asyncio
from time import perf_counter

import pytest

from packages.storage import ApplicationSnapshot, ToolCall
from packages.tools import batch_browser_operations as batch
from packages.tools.application_review_run import _response
from packages.tools.application_review_summary import (
    RETAINED_REASONS, review_presentation_summary, review_result_presentation,
)
from packages.tools.batch_browser_operations import ApplicationStatusResult
from packages.tools.application_identity_binding import identity_candidates
from packages.storage.models import ApplicationIdentityBinding
from tests.test_batch_browser_operations import _observed, _repository
from tests.test_application_status_model_fallback import _case, _card, FakeClient


def _row(reason, **extra):
    return ApplicationStatusResult(application_id="app", state="unresolved", reason=reason,
                                   elapsed_ms=0, **extra)


@pytest.mark.parametrize("reason", sorted(RETAINED_REASONS))
def test_only_presentation_changes_for_retained_reasons(reason):
    original = _row(reason, saved_stage="interview1", observed_label="官网原文")
    result = review_result_presentation(original)
    assert result.presentation_state == "retained"
    assert result.state == "unresolved" and result.reason == reason
    assert result.saved_stage == "interview1" and result.observed_label == "官网原文"
    assert result.wrote is False and original.presentation_state is None
    assert review_presentation_summary([result])["retained_by_stage"] == {"interview1": 1}


@pytest.mark.parametrize("reason", [
    "model_timeout", "model_invalid_output", "model_unavailable", "model_identity_mismatch",
    "model_quote_not_found", "visual_target_evidence_incomplete", "status_evidence_conflict", "frame_scope_denied",
    "application_page_unavailable", "status_evidence_missing", "unexpected_new_reason",
])
def test_real_limitations_are_not_hidden_even_with_stale_presentation_flag(reason):
    result = review_result_presentation(_row(reason, presentation_state="retained"))
    assert result.presentation_state is None
    summary = review_presentation_summary([result])
    assert summary["retained_count"] == 0 and summary["attention_required_count"] == 1


@pytest.mark.parametrize("state", ["updated", "unchanged", "blocked", "failed", "excluded"])
def test_retained_reason_cannot_override_other_states(state):
    row = _row("unparsed_page").model_copy(update={"state": state, "presentation_state": "retained"})
    assert review_result_presentation(row).presentation_state is None
    assert review_result_presentation(_row("unparsed_page", wrote=True)).presentation_state is None


def test_users_72_retained_results_complete_without_false_verification_or_extra_counts():
    rows = []
    for reason, count in {"record_present_status_unknown": 48, "unparsed_page": 19,
                          "target_record_not_matched": 3, "status_unmapped": 2}.items():
        for _ in range(count):
            # Old checkpoints need no rewrite or rerun to get the new projection.
            rows.append({"application_id": str(len(rows)), "state": "unresolved", "reason": reason,
                         "elapsed_ms": 0, "operation_id": "observed-this-run"})
    ids = [row["application_id"] for row in rows]
    state = {"ids": ids + ids[:1], "results": {row["application_id"]: row for row in rows},
             "database_total": 72, "excluded_terminal": 0, "pages_total": 60, "run_status": "completed"}
    state["results"]["out-of-scope"] = {**rows[0], "reason": "target_record_not_matched"}
    response = _response("status-review-test", state, perf_counter())
    summary = response.summary
    assert summary["scope_complete"] and summary["completed_count"] == 72
    assert summary["remaining_count"] == summary["retryable_count"] == 0
    assert summary["retained_count"] == summary["unchanged_or_retained_count"] == 72
    assert summary["retained_by_stage"] == {"unknown": 72}  # Never invent a stage for legacy rows.
    assert summary["attention_required_count"] == 0
    assert summary["verification_success_count"] == summary["write_count"] == 0
    assert not response.success and not response.unchanged and len(response.unresolved) == 72
    assert all(row.presentation_state == "retained" for row in response.unresolved)
    cards = summary["identity_confirmation_items"]
    assert len(cards) == 3 and all(card["operation_id"] == "observed-this-run" for card in cards)
    assert len({card["application_id"] for card in cards}) == 3
    assert sum(sum(reasons.values()) for reasons in summary["reason_breakdown"].values()) == 72
    assert all("presentation_state" not in row for row in state["results"].values())


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer"])
@pytest.mark.parametrize("full_review", [False, True])
def test_direct_and_checkpointed_review_preserve_saved_stage_and_history(tmp_path, monkeypatch, stage, full_review):
    repository = _repository(tmp_path, [{"id": "app", "title": "开发工程师", "stage": stage,
                                        "record_url": "https://ats.example/applications"}])
    requests = []

    async def observe(request, *_):
        requests.append(request)
        return _observed({"page": {"url": "https://ats.example/applications", "text": "我的投递"},
                          "entries": [], "application_records": []}, "observation-from-this-review")

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    request = batch.BatchObserveApplicationStatusInput(**(
        {"all_non_terminal": True} if full_review else {"application_ids": ["app"]}))
    result = asyncio.run(batch.batch_observe_application_status(request, object(), repository))
    assert result.summary["retained_count"] == 1 and result.summary["retained_by_stage"] == {stage: 1}
    assert result.summary["scope_complete"]
    assert result.summary["verification_success_count"] == 0
    assert result.unresolved[0].company_name == "示例公司"
    assert result.unresolved[0].saved_stage == stage
    with repository.storage.session() as session:
        application = session.get(ApplicationSnapshot, "app")
        assert application.stage == stage and application.stage_history == []
        if full_review:
            checkpoint = session.get(ToolCall, result.summary["run_id"])
            assert checkpoint.arguments["results"]["app"]["saved_stage"] == stage
    if full_review:
        resumed = asyncio.run(batch.batch_observe_application_status(
            batch.BatchObserveApplicationStatusInput(run_id=result.summary["run_id"]), object(), repository))
        assert resumed.summary["retained_count"] == 1
        assert len(requests) == 1  # Neutral output does not cause blind retries.


def test_mixed_results_have_a_disjoint_user_summary():
    rows = [
        _row("record_present_status_unknown", saved_stage="applied"),
        _row("target_record_not_matched", saved_stage="written"),
        _row("model_timeout"),
    ]
    for state in ["updated", "unchanged", "blocked", "failed", "excluded"]:
        rows.append(_row("other").model_copy(update={"state": state}))
    summary = review_presentation_summary(rows)
    assert summary["retained_count"] == 2
    assert summary["unchanged_or_retained_count"] == 3
    assert summary["attention_required_count"] == 1
    assert len(summary["identity_confirmation_items"]) == 1  # A subset, not a ninth result.
    assert summary["unchanged_or_retained_count"] + summary["attention_required_count"] + 4 == len(rows)


def test_model_identity_mismatch_is_confirmable_without_hiding_model_attention():
    row = _row("model_identity_mismatch", company_name="示例公司", job_title="AI工程师第",
               saved_stage="applied", operation_id="audited-vision-operation")
    summary = review_presentation_summary([row])
    assert summary["attention_required_count"] == 1
    assert summary["retained_count"] == summary["unchanged_or_retained_count"] == 0
    assert review_result_presentation(row).presentation_state is None
    [confirmation] = summary["identity_confirmation_items"]
    assert confirmation == {
        "application_id": "app", "company_name": "示例公司", "job_title": "AI工程师第",
        "reason": "model_identity_mismatch", "operation_id": "audited-vision-operation",
    }


def test_identity_confirmation_receipt_uses_real_operation_and_cannot_bind_itself(tmp_path, monkeypatch):
    repository, _, client, run, operations = _case(tmp_path, monkeypatch,
        cards=[_card("AI应用开发工程师（AI Coding方向）", "已投递")])
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "24").job_title = "软件工程师"
    response = run()
    assert response.unresolved[0].reason == "target_record_not_matched"
    [item] = response.summary["identity_confirmation_items"]
    assert item["job_title"] == "软件工程师" and item["operation_id"] == operations[0]
    candidates = identity_candidates(repository.storage, item["application_id"], operation_id=item["operation_id"])
    assert candidates["operation_id"] == operations[0]
    assert candidates["candidates"][0]["raw_title"] == "AI应用开发工程师（AI Coding方向）"
    assert candidates["candidates"][0]["selectable"] and not client.calls
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "applied"
        assert session.get(ApplicationIdentityBinding, "24") is None


def test_uncertain_model_keeps_current_stage_and_raw_label(tmp_path, monkeypatch):
    def uncertain(proposal):
        proposal["candidates"][0]["uncertainties"] = ["不能确定当前步骤"]
        return proposal
    repository, _, _, run, _ = _case(tmp_path, monkeypatch, cards=[_card("开发工程师", "审批排队")],
                                    stage="interview1", client=FakeClient(uncertain))
    response = run()
    assert response.unresolved[0].reason == "model_uncertain"
    assert response.unresolved[0].observed_label == "审批排队"
    assert response.summary["retained_by_stage"] == {"interview1": 1}
    assert response.summary["verification_success_count"] == 0
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "interview1"
