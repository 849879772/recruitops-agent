"""Pause/cancel must stop retries as well as admission of the next company."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.storage.models import TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools import batch_browser_operations as batch
from tests.test_application_review_run import _repository, _review_input


@pytest.mark.parametrize("control,status", [("cancel", "cancelling"), ("pause", "pausing")])
def test_control_received_after_first_dom_timeout_prevents_new_browser_read(tmp_path, monkeypatch, control, status):
    repository = _repository(tmp_path, 1)
    calls = []

    async def observe(request, *_):
        calls.append(request)
        assert len(calls) == 1, "must not dispatch a second browser read after control"
        with repository.storage.write_transaction() as session:
            checkpoint = session.scalar(select(ToolCall))
            checkpoint.arguments = {**checkpoint.arguments, "control_request": control, "run_status": status}
            session.get(TaskRun, checkpoint.task_id).status = status
        return SimpleNamespace(timed_out=True, error_code="timeout", data=SimpleNamespace(
            error_code="timeout", status="FAILED", observation=None, result={}, operation_id="first"))

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))
    assert len(calls) == 1
    assert result.summary["run_status"] == ("cancelled" if control == "cancel" else "paused")
