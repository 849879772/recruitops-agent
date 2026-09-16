from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from packages.approval import (
    ApprovalPreview,
    ApprovalRegistry,
    ApprovalStatus,
    PolicyErrorCode,
    SqlAlchemyApprovalPersistence,
)
from packages.storage import Storage


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)


class _BarrierPersistence(SqlAlchemyApprovalPersistence):
    def __init__(self, storage: Storage, barrier: Barrier) -> None:
        super().__init__(storage)
        self._barrier = barrier
        self._synchronized = False

    def get(self, token_id: str):
        state = super().get(token_id)
        if not self._synchronized:
            self._synchronized = True
            self._barrier.wait(timeout=5)
        return state


class _IssueBarrierPersistence(SqlAlchemyApprovalPersistence):
    def __init__(self, storage: Storage, barrier: Barrier) -> None:
        super().__init__(storage)
        self._barrier = barrier
        self._load_count = 0

    def load(self):
        state = super().load()
        self._load_count += 1
        if self._load_count == 2:
            self._barrier.wait(timeout=5)
        return state


def _preview(idempotency_key: str) -> ApprovalPreview:
    return ApprovalPreview(
        task_id=f"task-{idempotency_key}",
        operation="schedule_create",
        idempotency_key=idempotency_key,
        evidence_summary="The operator reviewed the schedule draft.",
        expires_at=NOW + timedelta(hours=1),
        payload={"event_date": "2026-08-20"},
    )


def _seed(path, *, approved: bool) -> str:
    storage = Storage.from_url(f"sqlite:///{path}")
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    issued = registry.issue(_preview("schedule:concurrency"), now=NOW)
    assert issued.token is not None
    if approved:
        decision = registry.approve(issued.token.token_id, now=NOW)
        assert decision.allowed is True
    return issued.token.token_id


def test_two_workers_can_consume_a_persistent_token_only_once(tmp_path) -> None:
    database = tmp_path / "approval.db"
    token_id = _seed(database, approved=True)
    barrier = Barrier(2)
    registries = [
        ApprovalRegistry(
            _BarrierPersistence(
                Storage.from_url(f"sqlite:///{database}"),
                barrier,
            )
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as workers:
        decisions = list(
            workers.map(
                lambda registry: registry.consume(token_id, now=NOW),
                registries,
            )
        )

    assert sum(decision.allowed for decision in decisions) == 1
    loser = next(decision for decision in decisions if not decision.allowed)
    assert loser.error_code is PolicyErrorCode.TOKEN_ALREADY_CONSUMED

    persisted = SqlAlchemyApprovalPersistence(Storage.from_url(f"sqlite:///{database}")).get(
        token_id
    )
    assert persisted is not None
    assert persisted[0].status is ApprovalStatus.CONSUMED
    assert persisted[0].consumed is True


def test_two_workers_can_claim_a_persistent_token_only_once(tmp_path) -> None:
    database = tmp_path / "approval-claim.db"
    token_id = _seed(database, approved=True)
    barrier = Barrier(2)
    registries = [
        ApprovalRegistry(
            _BarrierPersistence(
                Storage.from_url(f"sqlite:///{database}"),
                barrier,
            )
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as workers:
        decisions = list(
            workers.map(
                lambda registry: registry.begin(token_id, now=NOW),
                registries,
            )
        )

    assert sum(decision.allowed for decision in decisions) == 1
    loser = next(decision for decision in decisions if not decision.allowed)
    assert loser.error_code is PolicyErrorCode.TOKEN_IN_PROGRESS

    persisted = SqlAlchemyApprovalPersistence(Storage.from_url(f"sqlite:///{database}")).get(
        token_id
    )
    assert persisted is not None
    assert persisted[0].status is ApprovalStatus.EXECUTING
    assert persisted[0].consumed is False


def test_approve_and_reject_compete_without_overwriting_the_cas_winner(tmp_path) -> None:
    database = tmp_path / "approval.db"
    token_id = _seed(database, approved=False)
    barrier = Barrier(2)
    approve_registry = ApprovalRegistry(
        _BarrierPersistence(Storage.from_url(f"sqlite:///{database}"), barrier)
    )
    reject_registry = ApprovalRegistry(
        _BarrierPersistence(Storage.from_url(f"sqlite:///{database}"), barrier)
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        approved, rejected = workers.map(
            lambda action: action(),
            (
                lambda: approve_registry.approve(token_id, now=NOW),
                lambda: reject_registry.reject(token_id, now=NOW),
            ),
        )

    persisted = SqlAlchemyApprovalPersistence(Storage.from_url(f"sqlite:///{database}")).get(
        token_id
    )
    assert persisted is not None
    final_token = persisted[0]
    assert final_token.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}

    if final_token.status is ApprovalStatus.APPROVED:
        assert approved.allowed is True
        assert rejected.error_code is PolicyErrorCode.TOKEN_STATE_CONFLICT
    else:
        assert approved.error_code is PolicyErrorCode.TOKEN_REJECTED
        assert rejected.error_code is PolicyErrorCode.TOKEN_REJECTED


def test_two_workers_issue_the_same_binding_and_reuse_one_durable_token(tmp_path) -> None:
    database = tmp_path / "approval.db"
    preview = _preview("schedule:issue-race")
    Storage.from_url(f"sqlite:///{database}").initialize()
    barrier = Barrier(2)
    registries = [
        ApprovalRegistry(
            _IssueBarrierPersistence(Storage.from_url(f"sqlite:///{database}"), barrier)
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as workers:
        decisions = list(
            workers.map(
                lambda registry: registry.issue(preview, now=NOW),
                registries,
            )
        )

    assert all(decision.allowed for decision in decisions)
    assert all(decision.token is not None for decision in decisions)
    assert {decision.token.token_id for decision in decisions} == {
        decisions[0].token.token_id
    }

    persisted = SqlAlchemyApprovalPersistence(Storage.from_url(f"sqlite:///{database}")).load()
    assert len(persisted) == 1
    assert persisted[0][1] == preview


def test_issue_race_with_different_binding_returns_conflict_without_overwrite(tmp_path) -> None:
    database = tmp_path / "approval.db"
    first_preview = _preview("schedule:issue-conflict")
    second_preview = first_preview.model_copy(
        update={"payload": {"event_date": "2026-08-21"}}
    )
    Storage.from_url(f"sqlite:///{database}").initialize()
    barrier = Barrier(2)
    first_registry = ApprovalRegistry(
        _IssueBarrierPersistence(Storage.from_url(f"sqlite:///{database}"), barrier)
    )
    second_registry = ApprovalRegistry(
        _IssueBarrierPersistence(Storage.from_url(f"sqlite:///{database}"), barrier)
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.map(
            lambda request: request[0].issue(request[1], now=NOW),
            ((first_registry, first_preview), (second_registry, second_preview)),
        )

    assert sum(decision.allowed for decision in (first, second)) == 1
    conflict = second if not second.allowed else first
    assert conflict.error_code is PolicyErrorCode.TOKEN_STATE_CONFLICT
    winner_preview = first_preview if first.allowed else second_preview

    persisted = SqlAlchemyApprovalPersistence(Storage.from_url(f"sqlite:///{database}")).load()
    assert len(persisted) == 1
    assert persisted[0][1].payload == winner_preview.payload
