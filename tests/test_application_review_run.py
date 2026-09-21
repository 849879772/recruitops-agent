"""Deterministic checkpoint and concurrency tests for full application reviews."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, Storage
from packages.storage.models import TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools.batch_browser_operations import (
    ApplicationStatusResult,
    BatchObserveApplicationStatusInput,
    BatchObserveApplicationStatusResponse,
)
from packages.tools.typed import EvidenceSource, ToolErrorCode, ToolStatus


def _repository(tmp_path, count: int) -> PostgresRecruitmentRepository:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'review.db'}", initialize=True)
    with storage.write_transaction() as session:
        for index in range(count):
            application_id = str(index)
            session.add(ApplicationSnapshot(
                id=application_id,
                company_name="匿名示例公司",
                job_title=f"Example Engineer {index}",
                record_url=f"https://site{index}.example/applications",
                stage="applied",
                idempotency_key=f"application:{application_id}",
                stage_history=[],
                source="test",
                source_ref=application_id,
            ))
    return PostgresRecruitmentRepository(storage)


def _part(request, outcomes: dict[str, tuple[str, str | None]]):
    buckets = {
        name: []
        for name in ("updated", "unchanged", "excluded", "blocked", "unresolved", "failed")
    }
    for application_id in request.application_ids:
        state, reason = outcomes[application_id]
        buckets[state].append(ApplicationStatusResult(
            application_id=application_id,
            state=state,
            reason=reason,
            elapsed_ms=0,
        ))
    conclusive = sum(len(buckets[name]) for name in ("updated", "unchanged", "excluded"))
    status = ToolStatus.SUCCESS if conclusive == len(request.application_ids) else ToolStatus.AMBIGUOUS
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status",
        status=status,
        success=status is ToolStatus.SUCCESS,
        total=len(request.application_ids),
        pages_total=1,
        evidence=[EvidenceSource(source="anonymous.fixture")],
        error_code=None if status is ToolStatus.SUCCESS else ToolErrorCode.AMBIGUOUS_MATCH,
        error_message=None if status is ToolStatus.SUCCESS else "fixture outcome needs review",
        timeout_ms=request.timeout_per_application_ms,
        elapsed_ms=0,
        read_only=False,
        **buckets,
    )


def _review_input(run_id: str | None = None):
    return (BatchObserveApplicationStatusInput(all_non_terminal=True) if run_id is None
            else BatchObserveApplicationStatusInput(run_id=run_id))


def test_review_wave_caps_global_concurrency_and_serializes_child_batches(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 10)
    active = peak = 0

    async def observe(request, *_):
        nonlocal active, peak
        assert request.max_concurrent == 1
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return _part(request, {item: ("unchanged", None) for item in request.application_ids})
        finally:
            active -= 1

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))

    assert peak == 4
    assert result.summary["processed_count"] == 10
    assert result.summary["scope_complete"] is True
    assert result.summary["verification_success_count"] == 10


def test_review_resumes_across_ten_page_waves_with_unique_counts(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 12)
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        return _part(request, {item: ("unchanged", None) for item in request.application_ids})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)

    async def run():
        first = await review.continue_application_review(_review_input(), object(), repository)
        assert first.summary["unique_record_count"] == 12
        assert first.summary["processed_count"] == 10
        assert first.summary["completed_count"] == 10
        assert first.summary["remaining_count"] == 2
        assert first.summary["scope_complete"] is False
        run_id = first.summary["run_id"]

        second = await review.continue_application_review(
            _review_input(run_id), object(), repository,
        )
        assert second.summary["unique_record_count"] == 12
        assert second.summary["processed_count"] == 12
        assert second.summary["completed_count"] == 12
        assert second.summary["scope_complete"] is True
        assert second.summary["verification_success_count"] == 12
        assert second.success is True

    asyncio.run(run())
    assert len(visited) == 12
    assert len(set(visited)) == 12


def test_review_retries_transient_failure_for_same_run_id(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 1)
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        if len(visited) == 1:
            return _part(request, {"0": ("failed", "browser_busy")})
        return _part(request, {"0": ("unchanged", None)})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)

    async def run():
        first = await review.continue_application_review(_review_input(), object(), repository)
        assert first.summary["processed_count"] == 1
        assert first.summary["completed_count"] == 0
        assert first.summary["remaining_count"] == 1
        assert first.summary["retryable_count"] == 1
        assert first.summary["scope_complete"] is False
        assert first.success is False

        second = await review.continue_application_review(
            _review_input(first.summary["run_id"]), object(), repository,
        )
        assert second.summary["processed_count"] == 1
        assert second.summary["completed_count"] == 1
        assert second.summary["scope_complete"] is True
        assert second.summary["verification_success_count"] == 1
        assert second.success is True

    asyncio.run(run())
    assert visited == ["0", "0"]


@pytest.mark.parametrize("reason", ["observation_timeout", "operation_failed"])
def test_review_stops_retrying_after_bounded_transient_attempts(tmp_path, monkeypatch, reason):
    repository = _repository(tmp_path, 1)
    visits = 0

    async def observe(request, *_):
        nonlocal visits
        visits += 1
        return _part(request, {"0": ("failed", reason)})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)

    async def run():
        result = await review.continue_application_review(_review_input(), object(), repository)
        run_id = result.summary["run_id"]
        assert result.summary["retryable_count"] == 1
        for _ in range(2):
            result = await review.continue_application_review(
                _review_input(run_id), object(), repository,
            )

        assert result.summary["scope_complete"] is True
        assert result.summary["completed_count"] == 1
        assert result.summary["retryable_count"] == 0
        assert result.summary["retry_exhausted_count"] == 1
        assert result.summary["completion_rate"] == 1.0
        assert result.summary["verification_rate"] == 0.0
        assert result.summary["failed"] == 1
        assert result.success is False

        replay = await review.continue_application_review(
            _review_input(run_id), object(), repository,
        )
        assert replay.summary["scope_complete"] is True
        assert replay.success is False

    asyncio.run(run())
    assert visits == 3


def test_review_does_not_retry_verified_login_or_missing_evidence(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 3)
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        outcomes = {
            "0": ("unchanged", None),
            "1": ("blocked", "login_required"),
            "2": ("unresolved", "status_evidence_missing"),
        }
        return _part(request, {item: outcomes[item] for item in request.application_ids})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)

    async def run():
        first = await review.continue_application_review(_review_input(), object(), repository)
        assert first.summary["scope_complete"] is True
        assert first.summary["verification_success_count"] == 1
        assert first.summary["verification_rate"] == 0.3333
        assert first.summary["blocked"] == 1
        assert first.summary["unresolved"] == 1
        assert first.success is False

        second = await review.continue_application_review(
            _review_input(first.summary["run_id"]), object(), repository,
        )
        assert second.summary["blocked"] == 1
        assert second.summary["unresolved"] == 1
        assert second.summary["verification_success_count"] == 1

    asyncio.run(run())
    assert len(visited) == 3
    assert set(visited) == {"0", "1", "2"}


def test_review_unique_count_deduplicates_repository_snapshot(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 2)
    original_list = repository.list_applications
    monkeypatch.setattr(repository, "list_applications", lambda: [*original_list(), *original_list()[:1]])
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        return _part(request, {item: ("unchanged", None) for item in request.application_ids})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))

    assert result.total == 2
    assert result.summary["unique_record_count"] == 2
    assert result.summary["database_total"] == 2
    assert result.summary["processed_count"] == 2
    assert len(visited) == 2
    assert len(set(visited)) == 2


def test_review_cancel_drains_children_before_releasing_checkpoint(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 10)

    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        entered_ids = set()
        cancelled_ids = set()

        async def observe(request, *_):
            entered_ids.update(request.application_ids)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_ids.update(request.application_ids)
                cancelled.set()

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        pending = asyncio.create_task(review.continue_application_review(
            _review_input(), object(), repository,
        ))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert cancelled.is_set()
        assert entered_ids == cancelled_ids
        with repository.storage.session() as session:
                checkpoint = session.scalar(select(ToolCall))
                task = session.get(TaskRun, checkpoint.task_id)
                assert checkpoint.arguments["lease_until"] == 0
                results = checkpoint.arguments["results"]
                assert set(results) == cancelled_ids
                assert all(item["reason"] == "review_wave_cancelled" for item in results.values())
                assert all(item["state"] == "failed" for item in results.values())
                assert all(checkpoint.arguments["attempts"][item] == 1 for item in cancelled_ids)
                assert task.status == "stopped"
                assert task.error_code == "review_wave_cancelled"

    asyncio.run(run())
