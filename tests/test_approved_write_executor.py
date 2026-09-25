from datetime import datetime, timedelta, timezone

import pytest

from packages.approval import (
    ApprovalPreview,
    ApprovalRegistry,
    ApprovalStatus,
    ApprovedWriteExecutor,
    OperationName,
    SqlAlchemyApprovalPersistence,
    WriteEffect,
)
from packages.storage import AgentStateStore, Storage


NOW = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[OperationName, dict[str, object]]] = []

    def _record(self, operation: OperationName, payload: dict[str, object]) -> WriteEffect:
        self.calls.append((operation, payload))
        return WriteEffect(
            before={"status": "before"},
            after={"status": "after", **payload},
            rollback_payload={"status": "before"},
        )

    def update_company_config(self, payload):
        return self._record(OperationName.COMPANY_CONFIG_UPDATE, payload)

    def update_crawler_recipe(self, payload):
        return self._record(OperationName.CRAWLER_RECIPE_UPDATE, payload)

    def create_application(self, payload):
        return self._record(OperationName.APPLICATION_CREATE, payload)

    def update_application_stage(self, payload):
        return self._record(OperationName.APPLICATION_STAGE_UPDATE, payload)

    def create_schedule(self, payload):
        return self._record(OperationName.SCHEDULE_CREATE, payload)

    def bind_recruitment_mail(self, payload):
        return self._record(OperationName.RECRUITMENT_MAIL_BINDING, payload)


def _preview(operation: OperationName, key: str) -> ApprovalPreview:
    values: dict[str, object] = {
        "task_id": f"task-{key}",
        "operation": operation,
        "idempotency_key": key,
        "evidence_summary": "Official evidence and user-confirmed preview.",
        "expires_at": NOW + timedelta(hours=1),
        "payload": {"target": key},
    }
    if operation is OperationName.APPLICATION_STAGE_UPDATE:
        values.update(current_stage="applied", target_stage="written")
    return ApprovalPreview(**values)


@pytest.mark.parametrize(
    "operation",
    [item for item in OperationName if item is not OperationName.BROWSER_ACTION],
)
def test_executor_dispatches_each_approved_operation_once_with_audit(operation) -> None:
    registry = ApprovalRegistry()
    preview = _preview(operation, f"key-{operation.value}")
    issued = registry.issue(preview, now=NOW)
    assert issued.token is not None
    approved = registry.approve(issued.token.token_id, now=NOW)
    assert approved.status is ApprovalStatus.APPROVED
    events: list[str] = []

    class TracingAdapter(RecordingAdapter):
        def _record(self, operation, payload):
            events.append("adapter")
            return super()._record(operation, payload)

    adapter = TracingAdapter()
    original_begin = registry.begin
    original_complete = registry.complete

    def begin(*args, **kwargs):
        events.append("begin")
        return original_begin(*args, **kwargs)

    def complete(*args, **kwargs):
        events.append("complete")
        return original_complete(*args, **kwargs)

    registry.begin = begin
    registry.complete = complete

    def before_write() -> None:
        events.append("backup")

    executor = ApprovedWriteExecutor(
        registry,
        adapter,
        before_write=before_write,
    )

    record = executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert record.success is True
    assert record.operation is operation
    assert record.before == {"status": "before"}
    assert record.rollback_payload == {"status": "before"}
    assert adapter.calls == [(operation, {"target": f"key-{operation.value}"})]
    assert events == ["begin", "backup", "adapter", "complete"]
    assert registry.token(issued.token.token_id).status is ApprovalStatus.CONSUMED

    with pytest.raises(PermissionError, match="token_already_consumed"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)


def test_browser_action_cannot_use_the_generic_write_executor() -> None:
    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.BROWSER_ACTION, "browser-1"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    adapter = RecordingAdapter()
    executor = ApprovedWriteExecutor(registry, adapter)

    with pytest.raises(PermissionError, match="dedicated browser endpoint"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert registry.token(issued.token.token_id).consumed is False
    assert adapter.calls == []


def test_executor_never_calls_adapter_before_human_approval() -> None:
    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "schedule-1"), now=NOW)
    assert issued.token is not None
    adapter = RecordingAdapter()
    backups: list[str] = []
    executor = ApprovedWriteExecutor(
        registry,
        adapter,
        before_write=lambda: backups.append("backup"),
    )

    with pytest.raises(PermissionError, match="token_not_approved"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert adapter.calls == []
    assert backups == []


def test_failed_adapter_is_audited_without_leaking_exception_text() -> None:
    class FailingAdapter(RecordingAdapter):
        def create_schedule(self, payload):
            raise RuntimeError("secret failure detail")

    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "schedule-fail"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    executor = ApprovedWriteExecutor(registry, FailingAdapter())

    with pytest.raises(RuntimeError, match="inspect the audit record"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    record = executor.records()[0]
    assert record.success is False
    assert record.error_code == "RuntimeError"
    assert "secret" not in record.model_dump_json()
    assert registry.token(issued.token.token_id).status is ApprovalStatus.APPROVED


def test_failed_backup_is_audited_and_leaves_the_token_retryable() -> None:
    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "backup-fail"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    received = []

    def failing_backup() -> None:
        raise OSError("backup unavailable")

    adapter = RecordingAdapter()
    executor = ApprovedWriteExecutor(
        registry,
        adapter,
        before_write=failing_backup,
        audit_sink=received.append,
    )

    with pytest.raises(RuntimeError, match="backup failed; inspect the audit record"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert registry.token(issued.token.token_id).status is ApprovalStatus.APPROVED
    assert adapter.calls == []
    assert len(received) == 1
    assert received[0].success is False
    assert received[0].error_code == "OSError"


def test_adapter_failure_can_retry_without_a_second_successful_write() -> None:
    class FlakyAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def create_schedule(self, payload):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient adapter failure")
            return self._record(OperationName.SCHEDULE_CREATE, payload)

    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "adapter-retry"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    adapter = FlakyAdapter()
    executor = ApprovedWriteExecutor(registry, adapter)

    with pytest.raises(RuntimeError, match="adapter failed; inspect the audit record"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    record = executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert record.success is True
    assert adapter.attempts == 2
    assert len(adapter.calls) == 1
    assert registry.token(issued.token.token_id).status is ApprovalStatus.CONSUMED


def test_durable_audit_sink_keeps_failed_attempt_when_retry_succeeds(tmp_path) -> None:
    from sqlalchemy import select

    from packages.storage import WriteAudit

    class FlakyAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def create_schedule(self, payload):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient adapter failure")
            return self._record(OperationName.SCHEDULE_CREATE, payload)

    storage = Storage.from_url(f"sqlite:///{tmp_path / 'audit.db'}", initialize=True)
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "durable-retry"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    executor = ApprovedWriteExecutor(
        registry,
        FlakyAdapter(),
        audit_sink=AgentStateStore(storage).save_write_audit,
    )

    with pytest.raises(RuntimeError, match="adapter failed; inspect the audit record"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)
    assert executor.execute(issued.token.token_id, operator="local-user", now=NOW).success is True

    with storage.session() as session:
        audits = session.scalars(select(WriteAudit)).all()
    assert len(audits) == 2
    assert {audit.success for audit in audits} == {False, True}


def test_concurrent_executor_claims_allow_only_one_adapter_call() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    class BlockingAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        def create_schedule(self, payload):
            self.calls.append((OperationName.SCHEDULE_CREATE, payload))
            self.started.set()
            assert self.release.wait(timeout=5)
            return WriteEffect(after={"status": "after"})

    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "claim-race"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    adapter = BlockingAdapter()
    first = ApprovedWriteExecutor(registry, adapter)
    second = ApprovedWriteExecutor(registry, adapter)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first_future = workers.submit(
            first.execute,
            issued.token.token_id,
            operator="worker-1",
            now=NOW,
        )
        assert adapter.started.wait(timeout=5)
        second_future = workers.submit(
            second.execute,
            issued.token.token_id,
            operator="worker-2",
            now=NOW,
        )
        with pytest.raises(PermissionError, match="token_in_progress"):
            second_future.result(timeout=5)
        adapter.release.set()
        assert first_future.result(timeout=5).success is True

    assert len(adapter.calls) == 1


def test_executor_forwards_successful_audit_to_durable_sink() -> None:
    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "sink-1"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    received = []
    executor = ApprovedWriteExecutor(
        registry,
        RecordingAdapter(),
        audit_sink=received.append,
    )

    record = executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert received == [record]


def test_executor_forwards_failed_audit_to_durable_sink() -> None:
    class FailingAdapter(RecordingAdapter):
        def create_schedule(self, payload):
            raise RuntimeError("adapter failed")

    registry = ApprovalRegistry()
    issued = registry.issue(_preview(OperationName.SCHEDULE_CREATE, "sink-fail"), now=NOW)
    assert issued.token is not None
    registry.approve(issued.token.token_id, now=NOW)
    received = []
    executor = ApprovedWriteExecutor(
        registry,
        FailingAdapter(),
        audit_sink=received.append,
    )

    with pytest.raises(RuntimeError, match="inspect the audit record"):
        executor.execute(issued.token.token_id, operator="local-user", now=NOW)

    assert len(received) == 1
    assert received[0].success is False
