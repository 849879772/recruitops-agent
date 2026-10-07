"""Mail-only applications never consume a browser review or page write."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationStatus
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, Storage
from packages.storage.models import TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools import batch_browser_operations as batch
from packages.tools.application_status_evidence import (
    VerifyApplicationStatusEvidenceInput,
    verify_application_status_evidence,
)
from packages.tools.application_status_update import ApplicationStatusUpdateInput, update_application_status
from packages.tools.browser_bridge import (
    ObserveApplicationStatusPageInput,
    ReviewAndUpdateApplicationStatusInput,
    observe_application_status_page,
    observe_application_status_page_workflow,
    review_and_update_application_status_workflow,
)
from packages.tools.browser_status_update import BrowserStatusUpdateInput, browser_status_update


def _repository(tmp_path, urls):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'mail-only.db'}", initialize=True)
    with storage.write_transaction() as session:
        for application_id, url in urls.items():
            session.add(ApplicationSnapshot(
                id=application_id, company_name="Example", job_title=f"Engineer {application_id}",
                record_url=url, stage="applied", idempotency_key=f"application:{application_id}",
                stage_history=[], source="fixture", source_ref=application_id,
            ))
    return PostgresRecruitmentRepository(storage)


def _observer(monkeypatch):
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        observation = {
            "page": {"url": request.application_url},
            "application_records": [
                {"title": f"Engineer {item}", "context": f"Engineer {item} 当前状态: 申请成功",
                 "status": "applied", "label": "申请成功", "signals": {"has_explicit_status": True}}
                for item in request.application_ids
            ],
        }
        return SimpleNamespace(success=True, error_code=None, data=SimpleNamespace(
            operation_id="observed-fixture", status=OperationStatus.SUCCEEDED,
            error_code=None, observation=observation,
        ))

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    return visited


@pytest.mark.parametrize("full_review", [False, True])
def test_all_mail_only_records_are_excluded_without_a_bridge(tmp_path, monkeypatch, full_review):
    urls = {"empty": None, "blank": " ", "invalid": "not a URL",
            "email": "mailto:recruit@example.com", "credentials": "https://user:secret@example.com/status"}
    repository = _repository(tmp_path, urls)
    visited = _observer(monkeypatch)
    request = batch.BatchObserveApplicationStatusInput(
        all_non_terminal=True,
    ) if full_review else batch.BatchObserveApplicationStatusInput(application_ids=list(urls))

    result = asyncio.run(batch.batch_observe_application_status(request, None, repository))

    assert result.success
    assert visited == []
    assert result.total == len(urls) and result.pages_total == 0
    assert result.failed == result.unresolved == []
    assert result.summary["excluded_mail_only"] == len(urls)
    assert result.summary["completion_rate"] == 1
    assert all(item.reason == "mail_only" and not item.wrote for item in result.excluded)
    if full_review:
        assert result.summary["completed_count"] == len(urls)
        assert result.summary["remaining_count"] == 0
        with repository.storage.session() as session:
            checkpoint = session.get(ToolCall, result.summary["run_id"])
            task = session.get(TaskRun, result.summary["run_id"])
            assert set(checkpoint.arguments["ids"]) == set(urls)
            assert checkpoint.arguments["attempts"] == {}
            assert task.status == "completed" and task.current_step == f"{len(urls)}/{len(urls)}"
        replay = asyncio.run(batch.batch_observe_application_status(
            batch.BatchObserveApplicationStatusInput(run_id=result.summary["run_id"]), None, repository,
        ))
        assert replay.success and replay.summary["excluded_mail_only"] == len(urls)
        with repository.storage.session() as session:
            assert session.get(ToolCall, replay.summary["run_id"]).arguments["attempts"] == {}


@pytest.mark.parametrize("full_review", [False, True])
def test_mixed_selection_only_opens_saved_official_pages(tmp_path, monkeypatch, full_review):
    repository = _repository(tmp_path, {"official": "https://ats.example/status", "mail": None})
    visited = _observer(monkeypatch)
    request = batch.BatchObserveApplicationStatusInput(
        all_non_terminal=True,
    ) if full_review else batch.BatchObserveApplicationStatusInput(application_ids=["official", "mail"])

    result = asyncio.run(batch.batch_observe_application_status(request, object(), repository))

    assert result.success
    assert visited == ["official"] and result.pages_total == 1
    assert result.summary["excluded_mail_only"] == 1
    assert result.failed == result.unresolved == []
    assert [item.application_id for item in result.excluded] == ["mail"]


def test_scope_external_mail_only_record_does_not_break_page_owner_scan(tmp_path, monkeypatch):
    repository = _repository(tmp_path, {"official": "https://ats.example/status", "outside-mail": None})
    visited = _observer(monkeypatch)

    result = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(application_ids=["official"]), object(), repository,
    ))

    assert result.success and visited == ["official"]
    assert [item.application_id for item in result.unchanged] == ["official"]
    assert result.failed == result.unresolved == result.excluded == []


@pytest.mark.parametrize("full_review", [False, True])
def test_bridge_outage_never_fails_mail_only_records(tmp_path, monkeypatch, full_review):
    repository = _repository(tmp_path, {"official": "https://ats.example/status", "mail": None})
    visited = _observer(monkeypatch)
    request = batch.BatchObserveApplicationStatusInput(
        all_non_terminal=True,
    ) if full_review else batch.BatchObserveApplicationStatusInput(application_ids=["official", "mail"])

    result = asyncio.run(batch.batch_observe_application_status(request, None, repository))

    assert not result.success and visited == []
    assert result.summary["excluded_mail_only"] == 1
    assert [item.application_id for item in result.excluded] == ["mail"]
    assert [item.application_id for item in result.failed] == ["official"]
    assert result.unresolved == []


def test_resume_reclassifies_removed_urls_and_legacy_missing_url_outcomes(tmp_path, monkeypatch):
    repository = _repository(tmp_path, {
        "official": "https://ats.example/status", "removed": "https://other.example/status", "mail": None,
    })
    monkeypatch.setattr(review, "_WAVE_PAGES", 1)
    visited = _observer(monkeypatch)
    first = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
    ))
    assert first.summary["completed_count"] == 2 and first.summary["remaining_count"] == 1
    run_id = first.summary["run_id"]
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "removed").record_url = None
        checkpoint = session.get(ToolCall, run_id)
        state = dict(checkpoint.arguments)
        frozen_ids = list(state["ids"])
        assert set(frozen_ids) == {"official", "removed", "mail"}
        state["results"] = {**state["results"], "mail": {
            "application_id": "mail", "state": "unresolved",
            "reason": "record_url_missing_or_invalid", "elapsed_ms": 0,
        }}
        checkpoint.arguments = state

    resumed = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(run_id=run_id), None, repository,
    ))

    assert resumed.success and visited == ["official"]
    assert resumed.summary["excluded_mail_only"] == 2
    assert resumed.summary["completed_count"] == resumed.total == 3
    assert resumed.summary["remaining_count"] == 0 and resumed.pages_total == 1
    assert resumed.failed == resumed.unresolved == []
    with repository.storage.session() as session:
        checkpoint = session.get(ToolCall, run_id)
        assert checkpoint.arguments["ids"] == frozen_ids
        assert "removed" not in checkpoint.arguments["attempts"]


def test_adding_a_saved_url_allows_the_next_review(tmp_path, monkeypatch):
    repository = _repository(tmp_path, {"mail": None})
    visited = _observer(monkeypatch)
    request = batch.BatchObserveApplicationStatusInput(all_non_terminal=True)
    excluded = asyncio.run(batch.batch_observe_application_status(request, None, repository))
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "mail").record_url = "https://ats.example/status"

    reviewed = asyncio.run(batch.batch_observe_application_status(request, object(), repository))

    assert reviewed.success and visited == ["mail"]
    assert reviewed.summary["excluded_mail_only"] == 0
    assert reviewed.summary["run_id"] != excluded.summary["run_id"]


def test_offline_resume_saves_exclusions_without_consuming_official_page_attempts(tmp_path, monkeypatch):
    repository = _repository(tmp_path, {
        "first": "https://first.example/status", "removed": "https://removed.example/status",
        "pending": "https://pending.example/status",
    })
    monkeypatch.setattr(review, "_WAVE_PAGES", 1)
    visited = _observer(monkeypatch)
    first = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
    ))
    run_id = first.summary["run_id"]
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "removed").record_url = None

    resumed = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(run_id=run_id), None, repository,
    ))

    assert not resumed.success and visited == ["first"]
    assert resumed.summary["excluded_mail_only"] == 1
    assert resumed.summary["completed_count"] == 2 and resumed.summary["remaining_count"] == 1
    assert not resumed.summary["continuation_required"]
    assert resumed.failed == resumed.unresolved == []
    with repository.storage.session() as session:
        checkpoint = session.get(ToolCall, run_id)
        assert checkpoint.arguments["attempts"] == {"first": 1}
        assert checkpoint.arguments["results"]["removed"]["reason"] == "mail_only"


@pytest.mark.parametrize("workflow,input_type", [
    (observe_application_status_page_workflow, ObserveApplicationStatusPageInput),
    (review_and_update_application_status_workflow, ReviewAndUpdateApplicationStatusInput),
])
@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("supplied_url", [None, "https://caller.example/invented"])
def test_single_review_cannot_override_a_missing_saved_url(tmp_path, workflow, input_type, online, supplied_url):
    repository = _repository(tmp_path, {"mail": None})
    store = BrowserBridgeStore(repository.storage)
    request = input_type(application_id="mail", application_url=supplied_url,
                         device_id="edge-fixture", idempotency_key="mail-only-review")

    result = asyncio.run(workflow(request, store if online else None, repository))

    assert not result.success
    assert "mail_only" in result.error_message and "仅通过邮件更新" in result.error_message
    assert store.fetch_unacked_outbox("edge-fixture") == []
    assert store.get_by_idempotency_key("mail-only-review") is None


def test_page_writers_reject_mail_only_applications(tmp_path):
    repository = _repository(tmp_path, {"mail": None})
    captured = datetime.now(timezone.utc)
    direct = browser_status_update(BrowserStatusUpdateInput(
        application_id="mail", page_url="https://caller.example/invented",
        terminal_result={"operation_id": "invented-operation", "status": "written", "label": "笔试中",
                         "entries": [{"status": "written", "label": "笔试中"}], "captured_at": captured},
    ), repository.storage)
    unified = update_application_status(ApplicationStatusUpdateInput(
        application_id="mail", evidence_type="page", evidence_id="invented-operation",
        target_status="written", observed_label="笔试中", evidence="Engineer mail 笔试中", captured_at=captured,
    ), repository, object())

    assert not direct.success and not unified.success
    assert direct.data.reason_code == unified.data.reason_code == "mail_only"
    assert not direct.data.wrote and not unified.data.wrote
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "mail").stage == "applied"


def test_old_page_evidence_is_rejected_after_saved_url_is_removed(tmp_path):
    page_url = "https://ats.example/status"
    repository = _repository(tmp_path, {"mail": page_url})
    store = BrowserBridgeStore(repository.storage)
    captured = datetime.now(timezone.utc)
    created = observe_application_status_page(ObserveApplicationStatusPageInput(
        application_id="mail", application_url=page_url, device_id="edge-fixture", idempotency_key="old-page",
    ), store)
    operation_id = created.data.operation_id
    dispatch = store.fetch_unacked_outbox("edge-fixture")[0]
    store.ack("edge-fixture", dispatch.sequence, operation_id=operation_id)
    store.append_event(operation_id, "validating", OperationStatus.VALIDATING)
    store.terminal_result(operation_id, {
        "application_id": "mail", "page_url": page_url, "captured_at": captured.isoformat(),
        "page": {"text": "Engineer mail 笔试中"},
    }, status=OperationStatus.SUCCEEDED)
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "mail").record_url = None

    result = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="mail", observation_operation_id=operation_id,
        observed_status="written", observed_label="笔试中", evidence="Engineer mail 笔试中",
        confidence=0.99, captured_at=captured,
    ), store)

    assert not result.success and "mail_only" in result.error_message
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "mail").stage == "applied"
