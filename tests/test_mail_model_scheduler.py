from datetime import datetime, timezone
from types import SimpleNamespace

import packages.scheduler.runtime as scheduler_runtime
from packages.scheduler import TaskContext, TaskType, build_runtime_task_handlers


def _context() -> TaskContext:
    return TaskContext(
        task_id=TaskType.RECRUITMENT_MAILBOX.value,
        task_label=TaskType.RECRUITMENT_MAILBOX.value,
        scheduled_for=datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc),
        run_id="run-mail-model",
        attempt=1,
    )


def _sync_result() -> SimpleNamespace:
    return SimpleNamespace(
        model_dump=lambda **_kwargs: {
            "fetched": 2,
            "inserted": 1,
            "reused": 1,
            "next_cursor": "cursor-2",
        }
    )


def test_scheduled_mailbox_syncs_then_calls_model_processor_once(monkeypatch) -> None:
    events: list[tuple[object, ...]] = []
    settings = SimpleNamespace(mail_enabled=True, llm_enabled=True)
    store = object()
    repository = object()
    processing = {
        "status": "partial",
        "processed": 2,
        "updated": 1,
        "unchanged": 0,
        "irrelevant": 0,
        "unresolved": 1,
        "failed": 0,
        "results": [{"mail_id": "mail-1", "status": "updated"}],
    }

    def sync(_settings, received_store, **kwargs):
        events.append(("sync", received_store, kwargs))
        return _sync_result()

    def process(received_store, received_repository, received_settings, *, limit):
        events.append(
            (
                "process",
                received_store,
                received_repository,
                received_settings,
                limit,
            )
        )
        return processing

    def legacy(*_args, **_kwargs):
        raise AssertionError("scheduled mailbox must not call the legacy updater")

    monkeypatch.setattr(scheduler_runtime, "sync_configured_mail", sync)
    monkeypatch.setattr(scheduler_runtime, "process_pending_mail", process)
    monkeypatch.setattr(scheduler_runtime, "_link_unambiguous_recruitment_mail", legacy)

    handlers = build_runtime_task_handlers(
        settings=settings,
        repository=repository,
        mail_store=store,
    )
    result = handlers[TaskType.RECRUITMENT_MAILBOX.value](_context())

    assert [event[0] for event in events] == ["sync", "process"]
    assert events[1][1:] == (store, repository, settings, 20)
    assert result["status"] == "synced"
    assert result["fetched"] == 2
    assert result["processing"] == processing
    assert result["processing_status"] == "partial"
    assert result["processed"] == 2
    assert result["updated"] == 1
    assert result["unresolved"] == 1


def test_disabled_llm_reports_blocked_processing_without_legacy_fallback(monkeypatch) -> None:
    events: list[str] = []
    settings = SimpleNamespace(mail_enabled=True, llm_enabled=False)
    processing = {
        "status": "blocked",
        "processed": 0,
        "updated": 0,
        "unchanged": 0,
        "irrelevant": 0,
        "unresolved": 0,
        "failed": 0,
        "results": [],
        "reason": "llm_disabled",
    }

    monkeypatch.setattr(
        scheduler_runtime,
        "sync_configured_mail",
        lambda *_args, **_kwargs: (events.append("sync") or _sync_result()),
    )
    monkeypatch.setattr(
        scheduler_runtime,
        "process_pending_mail",
        lambda *_args, **_kwargs: (events.append("process") or processing),
    )
    monkeypatch.setattr(
        scheduler_runtime,
        "_link_unambiguous_recruitment_mail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled LLM must not fall back to legacy regex processing")
        ),
    )

    handlers = build_runtime_task_handlers(
        settings=settings,
        repository=object(),
        mail_store=object(),
    )
    result = handlers[TaskType.RECRUITMENT_MAILBOX.value](_context())

    assert events == ["sync", "process"]
    assert result["processing"]["status"] == "blocked"
    assert result["processing_status"] == "blocked"
    assert result["processing"]["reason"] == "llm_disabled"
