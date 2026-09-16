from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from packages.security import redact_sensitive


class ExecutionTrace(BaseModel):
    """A redacted, structured record of one tool/model operation."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1)
    task_id: str | None = None
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_ms: int = Field(default=0, ge=0)
    success: bool | None = None
    timed_out: bool = False
    error_code: str | None = None
    token_input: int | None = Field(default=None, ge=0)
    token_output: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceRecorder(Protocol):
    def record(self, event: ExecutionTrace) -> None: ...


class FanoutTraceRecorder:
    """Write one already-structured event to multiple independent sinks."""

    def __init__(self, delegates: list[TraceRecorder]):
        if not delegates:
            raise ValueError("at least one trace delegate is required")
        self.delegates = tuple(delegates)

    def record(self, event: ExecutionTrace) -> None:
        for delegate in self.delegates:
            try:
                delegate.record(event)
            except OSError:
                # Observability degradation must not break the user operation.
                continue


class InMemoryTraceRecorder:
    """Deterministic sink for tests and local development."""

    def __init__(self) -> None:
        self.events: list[ExecutionTrace] = []

    def record(self, event: ExecutionTrace) -> None:
        self.events.append(event)


class RedactingTraceRecorder:
    """Apply recursive redaction before an event reaches any external sink."""

    def __init__(self, delegate: TraceRecorder):
        self.delegate = delegate

    def record(self, event: ExecutionTrace) -> None:
        safe_metadata = redact_sensitive(event.metadata)
        safe_error = redact_sensitive(event.error_code)
        self.delegate.record(
            event.model_copy(
                update={
                    "metadata": safe_metadata if isinstance(safe_metadata, dict) else {},
                    "error_code": safe_error if isinstance(safe_error, str) else None,
                }
            )
        )


class JsonlTraceRecorder:
    """Append already-redacted structured events for local observability tooling."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def record(self, event: ExecutionTrace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def read(self, *, limit: int = 200) -> list[ExecutionTrace]:
        """Read the newest valid records without failing on a partial final line."""

        if limit < 1 or not self.path.is_file():
            return []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        events: list[ExecutionTrace] = []
        for line in lines[-limit:]:
            try:
                events.append(ExecutionTrace.model_validate_json(line))
            except ValueError:
                continue
        return events


__all__ = [
    "ExecutionTrace",
    "FanoutTraceRecorder",
    "InMemoryTraceRecorder",
    "JsonlTraceRecorder",
    "RedactingTraceRecorder",
    "TraceRecorder",
]
