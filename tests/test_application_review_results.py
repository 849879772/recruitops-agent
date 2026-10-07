"""Offline pagination, bounded MCP receipts, and historical/latest separation."""

import asyncio
import base64
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
from time import perf_counter

import pytest
from sqlalchemy import select

from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, TaskRun, ToolCall
from packages.tools.application_review_results import (
    ApplicationReviewResultsInput as Input, application_review_results as query,
    compact_batch_response,
)
from packages.tools.application_review_run import _response
from packages.tools.application_review_tasks import (
    ApplicationReviewStatusInput, ApplicationReviewControlInput,
    application_review_status, control_application_review,
)
from packages.tools.typed import ToolErrorCode

RUN = "status-review-" + "a" * 32
NOW = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)
OUTCOMES = [
    ("unchanged", "verified"), ("updated", "forward_stage"), ("excluded", "mail_only"),
    ("failed", "readiness_timeout"), ("blocked", "login_required"),
    ("unresolved", "model_invalid_output"), ("unresolved", "record_present_status_unknown"),
    ("unresolved", "target_card_not_unique"),
]


@pytest.fixture
def repository(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'result-pages.db'}", initialize=True)
    repo = PostgresRecruitmentRepository(storage)
    with storage.write_transaction() as session:
        results = {}
        for index in range(240):
            app_id = f"app-{index:03}"
            state, reason = OUTCOMES[index % len(OUTCOMES)]
            result = {"application_id": app_id, "company_name": f"历史公司{index}", "job_title": f"历史岗位{index}",
                "state": state, "reason": reason, "saved_stage": "applied", "elapsed_ms": 100,
                "checked_at": NOW.isoformat(), "wrote": state == "updated", "model_disposition": "analyzed",
                "vision_disposition": "not_requested", "diagnostics": {"raw": "private diagnostic " * 1000},
                "observation": {"page": {"url": f"https://example.test/{index}", "text": "private page " * 1000}}}
            results[app_id] = result
            session.add(ApplicationSnapshot(id=app_id, company_name=f"当前公司{index}", job_title=f"当前岗位{index}",
                record_url=f"https://current.test/{index}", stage="applied", stage_history=[], source="fixture",
                idempotency_key=f"app:{index}", source_ref=app_id,
                last_review={"state": "unchanged", "reason": "newer_verified", "saved_stage": "applied", "checked_at": (NOW+timedelta(hours=1)).isoformat()}))
        session.add(TaskRun(id=RUN, task_type="application_status_review", status="completed", user_request="fixture",
                            source="fixture", source_ref=RUN, max_steps=1, updated_at=NOW))
        session.flush()
        session.add(ToolCall(id=RUN, task_id=RUN, tool_name="application_review_checkpoint", source="fixture", source_ref=RUN,
            updated_at=NOW, arguments={"ids": list(results), "results": results, "database_total": 240,
                "excluded_terminal": 0, "pages_total": 240, "run_status": "completed", "metadata": {"thread_id": "thread-a"}}))
    yield repo
    storage.engine.dispose()


def all_pages(repository, **params):
    rows, cursor = [], None
    while True:
        response = query(Input(**params, cursor=cursor, limit=17), repository)
        assert response.success, response.error_message
        assert response.read_only
        data = response.data
        rows.extend(data["items"])
        if not data["has_more"]:
            assert not data["next_cursor"]
            assert len(rows) == data["total"]
            return rows, data
        cursor = data["next_cursor"]


def test_all_240_results_are_available_and_aggregate_matches_summary(repository):
    rows, data = all_pages(repository, run_id=RUN, category="all")
    assert len(rows) == 240
    assert len({row["application_id"] for row in rows}) == 240
    counts = Counter(row["state"] for row in rows)
    assert all(data["summary"][state] == count for state, count in counts.items())
    assert all(row["company_name"].startswith("历史公司") and row["job_title"].startswith("历史岗位") for row in rows)
    assert all(row["name_source"] == "review_result" and row["record_url"].startswith("https://example.test") for row in rows)
    assert "private" not in json.dumps(rows)


@pytest.mark.parametrize("category,count", [("attention", 90), ("failed", 30), ("blocked", 30),
    ("unresolved", 90), ("retained", 60), ("unchanged", 30), ("updated", 30), ("excluded", 30)])
def test_category_counts_are_truthful(repository, category, count):
    rows, _ = all_pages(repository, category=category)
    assert len(rows) == count
    if category == "attention":
        assert not any(row["presentation_state"] == "retained" for row in rows)


def test_incomplete_visual_target_remains_named_and_actionable(repository):
    with repository.storage.write_transaction() as session:
        checkpoint = session.get(ToolCall, RUN)
        saved = json.loads(json.dumps(checkpoint.arguments))
        saved["results"]["app-003"].update(state="unresolved", reason="visual_target_evidence_incomplete",
            wrote=False, model_disposition="skipped", vision_disposition="analyzed")
        checkpoint.arguments = saved
    rows, data = all_pages(repository, run_id=RUN, category="attention", reason="visual_target_evidence_incomplete")
    assert len(rows) == data["total"] == 1
    row = rows[0]
    assert row["company_name"] == "历史公司3" and row["job_title"] == "历史岗位3"
    assert row["state"] == "unresolved" and row["presentation_state"] is None and not row["wrote"]
    assert row["reason"] == "visual_target_evidence_incomplete"


def test_reason_filter_and_thread_scope(repository):
    rows, _ = all_pages(repository, category="all", reason="target_card_not_unique", thread_id="thread-a")
    assert len(rows) == 30
    assert query(Input(thread_id="thread-b"), repository).error_code == ToolErrorCode.NOT_FOUND


def test_name_fallback_and_deleted_records_are_not_invented(repository):
    with repository.storage.write_transaction() as session:
        checkpoint = session.get(ToolCall, RUN)
        state = json.loads(json.dumps(checkpoint.arguments))
        for app_id in ("app-003", "app-004"):
            state["results"][app_id].update(company_name=None, job_title=None, observation=None)
        checkpoint.arguments = state
        session.delete(session.get(ApplicationSnapshot, "app-004"))
    rows, _ = all_pages(repository, category="attention")
    found = {row["application_id"]: row for row in rows}
    assert found["app-003"]["company_name"] == "当前公司3"
    assert found["app-003"]["name_source"] == "application_snapshot_fallback"
    assert found["app-003"]["url_source"] == "application_snapshot"
    assert found["app-004"]["company_name"] is None
    assert found["app-004"]["job_title"] is None
    assert found["app-004"]["name_source"] == "unavailable"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_expired_history_never_uses_newer_receipts(repository, status):
    with repository.storage.write_transaction() as session:
        checkpoint = session.get(ToolCall, RUN)
        summary = _response(RUN, checkpoint.arguments, perf_counter()).summary
        checkpoint.arguments = {"run_status": status, "details_expired": True, "summary": summary}
        session.get(TaskRun, RUN).status = status
    data = query(Input(run_id=RUN, category="all"), repository).data
    assert data["details_expired"] and data["items"] == [] and data["total"] is None
    assert data["summary"]["total"] == 240
    latest, latest_data = all_pages(repository, scope="latest", category="all")
    assert len(latest) == 240 and {row["reason"] for row in latest} == {"newer_verified"}
    assert latest_data["scope"] == "latest" and latest_data["run_id"] is None
    with pytest.raises(ValueError):
        Input(scope="latest", run_id=RUN)


def test_cancelled_results_remain_readable_but_control_discovery_excludes_terminal(repository):
    with repository.storage.write_transaction() as session:
        session.get(TaskRun, RUN).status = "cancelled"
    assert query(Input(), repository).data["run_status"] == "cancelled"
    status = application_review_status(ApplicationReviewStatusInput(), repository)
    assert status.success and status.data["run"]["run_id"] == RUN
    assert status.data["run"]["can_resume"] is False
    control = asyncio.run(control_application_review(ApplicationReviewControlInput(action="resume"), None, repository))
    assert not control.success and control.error_code == ToolErrorCode.NOT_FOUND


def test_cursor_pins_run_rejects_filters_and_changed_results(repository):
    first = query(Input(category="all", limit=2), repository).data
    cursor = first["next_cursor"]
    assert not query(Input(category="failed", cursor=cursor), repository).success
    assert not query(Input(cursor="not-base64"), repository).success
    malformed = base64.urlsafe_b64encode(json.dumps({"offset": 0, "run_id": [], "fingerprint": "a" * 24}).encode()).decode()
    assert query(Input(cursor=malformed), repository).error_code == ToolErrorCode.INVALID_INPUT
    new_run = "status-review-" + "b" * 32
    with repository.storage.write_transaction() as session:
        session.add(TaskRun(id=new_run, task_type="application_status_review", status="completed", user_request="new",
                            source="fixture", source_ref=new_run, updated_at=NOW+timedelta(hours=1)))
        session.flush()
        old = session.get(ToolCall, RUN)
        session.add(ToolCall(id=new_run, task_id=new_run, tool_name="application_review_checkpoint", source="fixture",
            source_ref=new_run, arguments=old.arguments, updated_at=NOW+timedelta(hours=1)))
    continued = query(Input(category="all", cursor=cursor), repository).data
    assert continued["run_id"] == RUN
    with repository.storage.write_transaction() as session:
        old = session.get(ToolCall, RUN)
        state = json.loads(json.dumps(old.arguments))
        state["results"]["app-000"]["reason"] = "changed"
        old.arguments = state
    assert query(Input(category="all", cursor=cursor), repository).error_code == ToolErrorCode.INVALID_INPUT


def test_large_batch_mcp_output_is_bounded_and_internal_evidence_is_intact(repository):
    with repository.storage.session() as session:
        state = session.get(ToolCall, RUN).arguments
    full = _response(RUN, state, perf_counter())
    compact = compact_batch_response(full)
    assert len(full.model_dump_json()) > 1_000_000
    assert len(compact.model_dump_json()) < 18_000
    assert compact.summary["results_has_more"]
    assert compact.summary["result_total"] == 240 and compact.summary["result_preview_count"] == 5
    assert len(compact.failed + compact.blocked + compact.unresolved) == 5
    assert "private" not in compact.model_dump_json()
    assert full.unchanged[0].observation["page"]["text"].startswith("private")


@pytest.mark.parametrize('reason,navigation', [
    ('desktop_navigation_changed', {'reason': 'will_redirect_cross_origin', 'phase': 'initial_load',
        'restriction': 'https_downgrade', 'sameOrigin': False, 'ssoCandidate': False,
        'requestedUrl': 'https://join.tencentmusic.com/applications?token=private',
        'attemptedUrl': 'http://join.tencentmusic.com/applications/?code=secret'}),
    ('authentication_recovery_timeout', {'reason': 'will_redirect_official_sso', 'phase': 'initial_load',
        'finalUrl': 'https://mozi-login.alibaba-inc.com/ssoLogin.htm?token=private',
        'authNavigation': {'provider': 'alibaba', 'hops': 1, 'returnedToRecruitment': False},
        'authWait': {'outcome': 'timeout', 'elapsedMs': 15003, 'budgetMs': 15000, 'progressCount': 0}}),
    ('authentication_recovery_timeout', {'reason': 'will_navigate_official_sso', 'phase': 'navigation_recovery',
        'finalUrl': 'https://uniportal.huawei.com/login?code=secret',
        'authNavigation': {'provider': 'huawei', 'hops': 2, 'returnedToRecruitment': False}}),
])
def test_real_failure_cause_survives_run_latest_and_mcp_without_page_secrets(repository, reason, navigation):
    from packages.storage.application_reviews import save_latest_reviews
    with repository.storage.write_transaction() as session:
        checkpoint = session.get(ToolCall, RUN)
        state = json.loads(json.dumps(checkpoint.arguments))
        row = state['results']['app-003']
        row.update(reason=reason, checked_at=(NOW+timedelta(hours=2)).isoformat(),
                   diagnostics={'navigation_diagnostics': navigation, 'raw': 'private page'})
        checkpoint.arguments = state
        save_latest_reviews(session, [row], checked_at=NOW+timedelta(hours=2), run_id=RUN)
    for request in (Input(run_id=RUN, reason=reason), Input(scope='latest', reason=reason)):
        response = query(request, repository)
        item = next(item for item in response.data['items'] if item['application_id'] == 'app-003')
        safe = item['navigation_diagnostics']
        assert safe['phase'] == navigation['phase']
        assert item['navigation_reason'] == navigation.get('restriction', navigation['reason'])
        if 'authNavigation' in navigation:
            assert safe['authNavigation'] == navigation['authNavigation']
        assert 'private' not in json.dumps(item) and 'secret' not in json.dumps(item)
    with repository.storage.session() as session:
        state = session.get(ToolCall, RUN).arguments
    full = _response(RUN, state, perf_counter())
    compact = compact_batch_response(full)
    projected = next(row for row in compact.failed if row.application_id == 'app-003')
    assert projected.observation is None
    assert set(projected.diagnostics) == {'navigation_diagnostics'}
    assert projected.diagnostics['navigation_diagnostics']['phase'] == navigation['phase']
    assert 'private' not in compact.model_dump_json() and 'secret' not in compact.model_dump_json()


def test_readonly_tool_registered_and_http_validation(repository, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main
    from packages.mcp.server import MCP_AGENT_TOOL_NAMES, TOOL_DEFINITIONS
    from packages.security import authorize_tool_call

    assert "application_review_results" in MCP_AGENT_TOOL_NAMES
    assert next(item for item in TOOL_DEFINITIONS if item.name == "application_review_results").read_only
    assert authorize_tool_call("application_review_results", read_only=True).allowed
    assert not authorize_tool_call("application_review_results", read_only=True, side_effect=True).allowed
    main.app.dependency_overrides[main.repository] = lambda: repository
    monkeypatch.delenv("RECRUITOPS_API_TOKEN", raising=False)
    try:
        client = TestClient(main.app)
        result = client.get("/api/applications/review-results", params={"limit": 5})
        assert result.status_code == 200, result.text
        assert len(result.json()["items"]) == 5
        assert client.get("/api/applications/review-results", params={"scope": "latest", "run_id": RUN}).status_code == 422
        assert client.get("/api/applications/review-results", params={"category": "incorrect"}).status_code == 422
        assert client.get("/api/applications/review-results", params={"run_id": "status-review-" + "f" * 32}).status_code == 404
    finally:
        main.app.dependency_overrides.pop(main.repository, None)
