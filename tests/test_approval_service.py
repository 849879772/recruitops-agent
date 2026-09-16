from datetime import datetime, timedelta, timezone

from packages.approval import (
    ApprovalPreview,
    ApprovalRegistry,
    ApprovalStatus,
    SqlAlchemyApprovalPersistence,
)
from packages.storage import Storage


def test_registry_exposes_preview_and_operator_decision_without_writing() -> None:
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    registry = ApprovalRegistry()
    preview = ApprovalPreview(
        task_id="task-approval-api",
        operation="schedule_create",
        idempotency_key="schedule:1",
        evidence_summary="User confirmed the schedule draft.",
        expires_at=now + timedelta(hours=1),
        payload={"event_date": "2026-08-20"},
    )

    issued = registry.issue(preview, now=now)
    assert issued.token is not None
    token_id = issued.token.token_id
    approved = registry.approve(token_id, now=now)

    assert approved.status is ApprovalStatus.APPROVED
    assert registry.preview(token_id) == preview
    assert registry.list()[0].status is ApprovalStatus.APPROVED


def test_registry_restores_approval_state_from_agent_storage(tmp_path) -> None:
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    preview = ApprovalPreview(
        task_id="task-durable-approval",
        operation="schedule_create",
        idempotency_key="schedule:durable",
        evidence_summary="The operator reviewed the schedule draft.",
        expires_at=now + timedelta(hours=1),
        payload={"event_date": "2026-08-20"},
    )

    issued = registry.issue(preview, now=now)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=now)

    restored = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    token = restored.token(issued.token.token_id)

    assert token.status is ApprovalStatus.APPROVED
    assert restored.preview(token.token_id) == preview
