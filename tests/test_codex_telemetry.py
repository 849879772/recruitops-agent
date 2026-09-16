from datetime import datetime, timezone

from packages.codex_runtime import CodexEventType
from packages.codex_runtime.events import CodexEvent
from packages.codex_runtime.telemetry import (
    CodexTelemetry,
    InMemoryTraceRecorder,
    JsonlTraceRecorder,
)


def _event(event_type: CodexEventType, **kwargs) -> CodexEvent:
    return CodexEvent(
        event_type=event_type,
        method=kwargs.pop("method", event_type.value),
        **kwargs,
    )


def test_event_trace_is_redacted_and_keeps_safe_metrics() -> None:
    recorder = InMemoryTraceRecorder()
    telemetry = CodexTelemetry(recorder, clock=lambda: 10.0)
    event = _event(
        CodexEventType.ITEM_COMPLETED,
        method="item/tool/completed",
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        text="完整邮件正文 secret@example.com",
        payload={
            "tool_name": "search_jobs",
            "usage": {"input_tokens": 12, "output_tokens": 5},
            "Authorization": "Bearer top-secret-token",
            "Cookie": "session=private-cookie",
            "email_body": "完整邮件正文 secret@example.com",
            "dom": "<html><body>private page</body></html>",
        },
    )

    trace = telemetry.record(event)
    serialized = trace.model_dump_json()

    assert trace.stage == "tool"
    assert trace.tool_name == "search_jobs"
    assert trace.input_tokens == 12
    assert trace.output_tokens == 5
    assert trace.total_tokens == 17
    assert "top-secret-token" not in serialized
    assert "private-cookie" not in serialized
    assert "secret@example.com" not in serialized
    assert "<html>" not in serialized
    assert "完整邮件正文" not in serialized
    assert "payload" not in serialized

    nested_tool = telemetry.record(
        _event(
            CodexEventType.ITEM_STARTED,
            method="item/started",
            thread_id="thread-1",
            turn_id="turn-1",
            item_id="item-2",
            payload={"item": {"type": "function_call", "name": "search_jobs"}},
        )
    )
    assert nested_tool.stage == "tool"
    assert nested_tool.tool_name == "search_jobs"


def test_span_latency_and_error_cannot_look_successful(tmp_path) -> None:
    recorder = InMemoryTraceRecorder()
    ticks = iter([1.0, 1.25, 1.5])
    telemetry = CodexTelemetry(recorder, clock=lambda: next(ticks))

    telemetry.record(
        _event(
            CodexEventType.TURN_STARTED,
            method="turn/started",
            thread_id="thread-1",
            turn_id="turn-1",
        )
    )
    completed = telemetry.record(
        _event(
            CodexEventType.TURN_COMPLETED,
            method="turn/completed",
            thread_id="thread-1",
            turn_id="turn-1",
            payload={"usage": {"input_tokens": 20, "output_tokens": 8}},
        )
    )
    error = telemetry.record(
        _event(
            CodexEventType.ERROR,
            method="turn/error",
            thread_id="thread-1",
            turn_id="turn-2",
            payload={"code": "provider_timeout", "message": "Bearer hidden"},
        )
    )

    assert completed.success is True
    assert completed.latency_ms == 250.0
    assert error.success is False
    assert error.phase == "error"
    assert error.error_code == "provider_timeout"
    assert "Bearer hidden" not in error.model_dump_json()

    jsonl = JsonlTraceRecorder(tmp_path / "codex-traces.jsonl")
    jsonl.record(completed)
    jsonl.record(error)
    assert [item.phase for item in jsonl.read()] == ["completed", "error"]


def test_trace_from_datetime_is_utc_and_recorder_is_injectable() -> None:
    recorder = InMemoryTraceRecorder()
    telemetry = CodexTelemetry(recorder)
    observed_at = datetime(2026, 8, 22, 1, 2, 3, tzinfo=timezone.utc)

    trace = telemetry.record(
        _event(
            CodexEventType.THREAD_STARTED,
            method="thread/started",
            thread_id="thread-1",
        ),
        timestamp=observed_at,
    )

    assert trace.observed_at == observed_at
    assert recorder.events == [trace]
