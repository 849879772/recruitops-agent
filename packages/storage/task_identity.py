"""Safe, explicit ownership fields for task receipts and progress projections."""

from collections.abc import Mapping


def task_identity(run_id: str, metadata=None, *, task_id: str | None = None) -> dict:
    """Never infer a conversation from the latest task or the active UI thread."""
    saved = metadata if isinstance(metadata, Mapping) else {}

    def identifier(value):
        return value if isinstance(value, str) and value.strip() and len(value) <= 255 else None

    return {
        "run_id": run_id,
        "task_id": identifier(saved.get("task_id")) or task_id or run_id,
        "thread_id": identifier(saved.get("thread_id")),
        "turn_id": identifier(saved.get("turn_id")),
    }
