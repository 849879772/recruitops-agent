"""Early mail-only receipt autoflush must not discard the page wave's claim."""

import asyncio

from packages.storage import ToolCall, TaskRun
from packages.tools import application_review_tasks, batch_browser_operations as batch
from tests.test_mail_only_application_review import _observer, _repository


def test_mail_only_autoflush_preserves_exclusions_and_active_claim(tmp_path, monkeypatch):
    repository = _repository(tmp_path, {"official": "https://ats.example/status", "mail": None})
    visited = _observer(monkeypatch)
    original = batch.observe_application_status_page_workflow
    claims = []

    async def inspect_claim(request, *args):
        storage, run_id, claim = application_review_tasks.REVIEW_CONTEXT.get()
        with storage.session() as session:
            checkpoint = session.get(ToolCall, run_id)
            claims.append(checkpoint.arguments["claim"])
            assert checkpoint.arguments["claim"] == claim
            assert checkpoint.arguments["results"]["mail"]["reason"] == "mail_only"
        return await original(request, *args)

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", inspect_claim)
    response = asyncio.run(batch.batch_observe_application_status(
        batch.BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository))
    assert response.success and len(claims) == 1 and visited == ["official"]
    with repository.storage.session() as session:
        checkpoint = session.get(ToolCall, response.summary["run_id"])
        task = session.get(TaskRun, response.summary["run_id"])
        assert checkpoint.arguments["results"]["mail"]["reason"] == "mail_only"
        assert checkpoint.arguments["results"]["official"]["state"] == "unchanged"
        assert checkpoint.arguments["attempts"] == {"official": 1}
        assert task.status == "completed"
