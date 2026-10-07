"""Deterministic identity and current-stage safety fixtures; no live services."""
import asyncio
from types import SimpleNamespace

import pytest

from packages.domain.application_identity import matching_records
from packages.domain.application_status_semantics import current_status_labels, timeline_without_current
from packages.storage import ApplicationSnapshot
from packages.tools import batch_browser_operations as batch
from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence
from packages.tools.browser_status_update import BrowserStatusUpdateInput, browser_status_update
from tests.test_application_status_model_fallback import _case, URL


@pytest.mark.parametrize(("target", "captured"), [
    ("AI应用工程师", "2027届-AI应用工程师"),
    ("测试开发工程师", "【校招】测试开发工程师"),
    ("C++开发工程师", "【27届校招】C++开发工程师"),
    ("2027届-AI应用工程师", "27届-AI应用工程师"),
    ("AI应用工程师", "【校招】2027届-AI应用工程师"),
    ("AI应用工程师（深圳）", "NO.2708 【校招】AI应用工程师（深圳）网申第一志愿"),
])
def test_bounded_recruitment_prefixes(target, captured):
    card = {"title": captured, "raw_title": captured}
    assert matching_records({"job_title": target}, [card]) == [card]


@pytest.mark.parametrize(("target", "captured"), [
    ("2026届-AI应用工程师", "2027届-AI应用工程师"),
    ("26届-AI应用工程师", "27届-AI应用工程师"),
    ("C++开发工程师", "27届--C++开发工程师"),
    ("27届-C++开发工程师", "27届--C++开发工程师"),
    ("C++开发工程师", "27届-C#开发工程师"),
    ("AI应用工程师（深圳）", "27届-AI应用工程师（北京）"),
    ("AI应用工程师", "27届-AI应用工程师（AI-Coding方向）"),
    ("AI应用工程师", "27届-AI应用工程师(J22541)"),
    ("AI应用工程师", "AI应用工程师-27届"),
])
def test_prefix_matching_preserves_identity_differences(target, captured):
    assert not matching_records({"job_title": target}, [{"title": captured}])


def test_raw_exact_precedes_prefix_candidates_and_cleaned_collisions_stay_ambiguous():
    exact = {"raw_title": "AI应用工程师", "title": "AI应用工程师"}
    a, b = {"title": "2027届-AI应用工程师"}, {"title": "【校招】AI应用工程师"}
    assert matching_records({"job_title": "AI应用工程师"}, [a, b, exact]) == [exact]
    assert matching_records({"job_title": "AI应用工程师"}, [a, b]) == [a, b]


@pytest.mark.parametrize("fault", [None, "unverified", "other_id", "other_page", "other_path", "wrong_site_id"])
def test_verified_alias_is_scoped_to_internal_owner_page_and_stable_id(fault):
    hint = {"verified": True, "application_id": "24", "page_url": URL,
            "raw_title": "具身智能创新应用工程师", "external_job_id": "ats-17"}
    if fault == "unverified": hint["verified"] = False
    if fault == "other_id": hint["application_id"] = "25"
    if fault == "other_page": hint["page_url"] = "https://other.example/applications"
    if fault == "other_path": hint["page_url"] = "https://ats.example/other-applications"
    if fault == "wrong_site_id": hint["external_job_id"] = "ats-18"
    app = {"id": "24", "job_title": "具身智能应用", "record_url": URL + "?session=ephemeral",
           "verified_identity_bindings": [hint]}
    card = {"title": hint["raw_title"], "job_id": "ats-17", "application_id": "site-24"}
    assert bool(matching_records(app, [card])) is (fault is None)


def test_verified_stable_id_disambiguates_same_title_and_never_falls_back():
    app = {"id": "24", "job_title": "AI应用工程师", "record_url": URL,
           "verified_identity_bindings": [{"verified": True, "application_id": "24", "page_url": URL,
               "raw_title": "AI应用工程师", "external_application_id": "site-1"}]}
    first = {"title": "AI应用工程师", "application_id": "site-1"}
    other = {"title": "AI应用工程师", "application_id": "site-2"}
    assert matching_records(app, [first, other]) == [first]
    assert not matching_records(app, [other])


LADDER = "2027届-AI应用工程师 查看详情 2026-09-27 1 申请成功 2 用人部门筛选 3 笔试 4 初试 5 复试"


def _unknown_card(kind):
    card = {"title": "2027届-AI应用工程师", "status": "", "label": "", "raw_status_labels": [],
            "context": "2027届-AI应用工程师 投递于2026-09-27", "evidence_source": "record-exists-only",
            "signals": {"has_active_step": False, "has_explicit_status": False}}
    if kind != "submission":
        card["context"] = LADDER
    if kind == "new_ladder":
        card.update(stage_labels=["申请成功", "用人部门筛选", "笔试", "初试", "复试"],
                    current_step_label="", evidence_source="timeline-without-current")
        card["signals"].update(has_progress_timeline=True, current_step_identified=False)
    if kind == "legacy_false_applied":
        card.update(status="applied", label="申请成功")
    return card


@pytest.mark.parametrize("kind", ["submission", "legacy_ladder", "new_ladder", "legacy_false_applied"])
@pytest.mark.parametrize("stage", ["applied", "written"])
def test_unselected_ladder_stays_unknown_but_dated_submission_is_a_baseline(tmp_path, monkeypatch, kind, stage):
    card = _unknown_card(kind)
    repository, store, client, run, ops = _case(tmp_path, monkeypatch, cards=[card], stage=stage)
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "24").job_title = "AI应用工程师"
    result = run()
    if kind == "submission":
        assert len(result.unchanged) == 1 and not result.unresolved
    else:
        assert [(item.state, item.reason) for item in result.unresolved] == [("unresolved", "record_present_status_unknown")]
        assert not result.unchanged
    assert not result.updated and not client.calls
    verified = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=ops[0], observed_status="applied",
        observed_label="申请成功" if kind != "submission" else "投递于2026-09-27", evidence=card["context"],
        confidence=1.0, captured_at="2026-09-28T01:00:00Z"), store)
    if kind == "submission":
        assert verified.success and verified.status == "unchanged"
    else:
        assert verified.error_code == "record_present_status_unknown"
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == stage


def test_explicit_success_still_confirms_unchanged(tmp_path, monkeypatch):
    card = {"title": "AI应用工程师", "status": "applied", "label": "申请成功", "context": "AI应用工程师 申请成功"}
    _, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[card])
    result = run()
    assert len(result.unchanged) == 1 and not result.unresolved and not client.calls


def test_whole_ladder_with_identified_current_is_verified_not_first_step(tmp_path, monkeypatch):
    card = _unknown_card("new_ladder")
    card.update(status="written", label="笔试", current_step_label="笔试", evidence_source="active-step", confidence=0.98)
    card["signals"].update(has_active_step=True, current_step_identified=True)
    assert not timeline_without_current(card) and current_status_labels(card) == ["笔试"]
    repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[card], entries=[card])
    result = run()
    assert len(result.updated) == 1 and not client.calls, result.model_dump()
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "written"


def test_direct_rule_verifier_rejects_legacy_whole_ladder(tmp_path, monkeypatch):
    card = _unknown_card("legacy_false_applied")
    repository, _, _, _, _ = _case(tmp_path, monkeypatch, cards=[card])
    result = browser_status_update(BrowserStatusUpdateInput(application_id="24", page_url=URL,
        terminal_result={"operation_status": "SUCCEEDED", "captured_at": "2026-09-28T01:00:00Z",
                         "entries": [{**card, "status": "written", "label": "笔试", "confidence": 1.0}]}), repository.storage)
    assert result.data.reason_code == "record_present_status_unknown"


def test_scope_external_prefix_collision_blocks_baseline(tmp_path, monkeypatch):
    card = {"title": "【校招】AI应用工程师", "status": "applied", "label": "申请成功", "context": "AI应用工程师 申请成功"}
    repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[card])
    with repository.storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="outside", company_name="示例公司", job_title="AI应用工程师",
            record_url=URL, stage="applied", idempotency_key="outside", stage_history=[], source="test", source_ref="outside"))
    result = run()
    assert result.unresolved[0].reason == "target_record_ambiguous"
    assert not result.unchanged and not client.calls


def test_unparsed_page_stays_unresolved_and_retains_bounded_diagnostics(tmp_path, monkeypatch):
    repository, store, client, _, _ = _case(tmp_path, monkeypatch)
    async def observe(*_):
        return SimpleNamespace(data=SimpleNamespace(error_code="UNPARSED_APPLICATION_PAGE", operation_id="fixture",
            status="STATE_UNCLEAR", observation=None, result={"page": {"text": "岗位记录页面"}, "semantic_nodes": [{"role": "main"}]}))
    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(application_ids=["24"]), store, repository))
    assert result.unresolved[0].reason == "unparsed_page"
    assert result.unresolved[0].diagnostics["page"]["text"] == "岗位记录页面"
    assert not client.calls


@pytest.mark.parametrize("path", ["baseline", "rule", "model"])
def test_persisted_manual_binding_flows_through_all_verification_paths(tmp_path, monkeypatch, path):
    from packages.storage.models import ApplicationIdentityBinding
    from packages.tools.application_identity_binding import _digest, _identity
    from packages.tools import application_status_model as model
    from tests.test_application_page_model_fallback import case as page_case, candidate
    label = "面试安排确认中" if path == "model" else "申请成功" if path == "baseline" else "笔试中"
    status = "" if path == "model" else "applied" if path == "baseline" else "written"
    card = {"title": "官网正式岗位名称", "raw_title": "NO.2708 官网正式岗位名称", "application_id": "site-application-7", "job_id": "site-job-3",
            "status": status, "label": label, "context": f"官网正式岗位名称 当前状态: {label}", "confidence": 0.97}
    if path == "model":
        # Keep the text resolver's binding contract independently testable;
        # production batch review now captures screenshots before model review.
        repository, store, operation, client, _ = page_case(tmp_path, monkeypatch,
            applications=[{"id": "24", "title": card["title"], "record_url": URL}],
            observation={"application_records": [card]},
            candidates=[candidate(card["title"], label=label, quote=card["context"], observed_status="interview")])
    else:
        repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[card], entries=[card] if path == "rule" else [])
    with repository.storage.write_transaction() as session:
        app = session.get(ApplicationSnapshot, "24")
        app.job_title = "用户保存的岗位别名"
        session.add(ApplicationIdentityBinding(application_id="24", state="bound", revision=1, page_url=URL,
            identity_digest=_digest(_identity(app)), approval_key="test-user-confirmed",
            card={"raw_title": card["raw_title"], "external_application_id": card["application_id"], "external_job_id": card["job_id"]}))
    if path == "model":
        result = asyncio.run(model.resolve_page_statuses(store, operation.operation_id,
            list(repository.list_applications()), {"24"}))["24"]
        assert result["state"] == "updated" and result["wrote"], result
    else:
        result = run()
        assert not result.unresolved and not result.failed, result.model_dump()
        assert len(result.unchanged if path == "baseline" else result.updated) == 1
    assert len(client.calls) == (1 if path == "model" else 0)
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == {"baseline": "applied", "rule": "written", "model": "interview1"}[path]


def test_normal_batch_uses_screenshot_review_not_text_model_fallback(tmp_path, monkeypatch):
    from packages import config
    from packages.tools import application_status_model as model
    from tests.test_batch_browser_operations import _repository, _observed
    repository = _repository(tmp_path, [{"id": "24", "title": "AI应用工程师", "record_url": URL}])
    requests, visual_calls = [], []
    observation = {"page": {"text": "AI应用工程师 当前状态：面试安排确认中"}, "entries": [],
                   "application_records": [], "semantic_nodes": []}
    async def observe(request, *_args):
        requests.append(request.include_vision)
        if request.include_vision:
            return _observed({**observation, "vision": {"text": observation["page"]["text"]}}, "visual-op")
        return _observed(observation, "dom-op")
    async def text_review(*_args):
        pytest.fail("normal batch must not use the text-only resolver")
    async def visual_review(_store, operation, _applications, targets):
        visual_calls.append(operation)
        return {key: {"state": "unchanged", "reason": "same_stage", "observed_status": "applied",
                      "wrote": False, "model_disposition": "called"} for key in targets}
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(write_enabled=True, llm_enabled=True,
        llm_api_key="fixture", vision_enabled=True, vision_model="deepseek-flash"))
    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_page_statuses", text_review)
    monkeypatch.setattr(model, "resolve_visual_statuses", visual_review)
    store = SimpleNamespace(get_events=lambda _: [
        SimpleNamespace(event_type="vision_request", payload={"provider_request_attempted": True, "image_count": 1}),
        SimpleNamespace(event_type="vision_analysis", payload={})])
    result = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(application_ids=["24"]), store, repository))
    assert requests == [False, True] and visual_calls == ["visual-op"]
    assert len(result.unchanged) == 1 and not result.updated and not result.unresolved
    assert result.summary["vision_analysis_count"] == 1
    assert repository.list_applications()[0].stage == "applied"
