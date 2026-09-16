from packages.observability import (
    ExecutionTrace,
    FanoutTraceRecorder,
    InMemoryTraceRecorder,
    JsonlTraceRecorder,
    RedactingTraceRecorder,
)


def test_tool_trace_is_structured_redacted_and_read_only() -> None:
    recorder = InMemoryTraceRecorder()
    recorder.record(
        ExecutionTrace(
            trace_id="trace-test",
            kind="tool",
            name="today_schedule",
            success=True,
            metadata={"read_only": True},
        )
    )

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.kind == "tool"
    assert event.name == "today_schedule"
    assert event.success is True
    assert event.metadata == {"read_only": True}


def test_trace_sink_redacts_tokens_before_storage() -> None:
    store = InMemoryTraceRecorder()
    recorder = RedactingTraceRecorder(store)

    recorder.record(
        ExecutionTrace(
            trace_id="redaction",
            kind="model",
            name="example",
            metadata={"Authorization": "Bearer top-secret-token"},
        )
    )

    serialized = store.events[0].model_dump_json()
    assert "top-secret-token" not in serialized
    assert "[REDACTED:authorization]" in serialized


def test_fanout_trace_recorder_writes_each_sink() -> None:
    first = InMemoryTraceRecorder()
    second = InMemoryTraceRecorder()
    event = ExecutionTrace(trace_id="trace-1", kind="tool", name="search_jobs")

    FanoutTraceRecorder([first, second]).record(event)

    assert first.events == [event]
    assert second.events == [event]


def test_fanout_trace_recorder_keeps_working_when_file_sink_is_unavailable() -> None:
    class UnavailableSink:
        def record(self, event):
            raise PermissionError("read-only mount")

    healthy = InMemoryTraceRecorder()
    event = ExecutionTrace(trace_id="trace-2", kind="tool", name="search_jobs")

    FanoutTraceRecorder([UnavailableSink(), healthy]).record(event)

    assert healthy.events == [event]


def test_jsonl_trace_recorder_restores_recent_valid_events(tmp_path) -> None:
    recorder = JsonlTraceRecorder(tmp_path / "traces.jsonl")
    recorder.record(ExecutionTrace(trace_id="trace-1", kind="tool", name="first"))
    recorder.record(ExecutionTrace(trace_id="trace-2", kind="tool", name="second"))
    with recorder.path.open("a", encoding="utf-8") as handle:
        handle.write("{partial")

    restored = recorder.read(limit=2)

    assert [event.trace_id for event in restored] == ["trace-2"]
