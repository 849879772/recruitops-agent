"""Actual scheduled-task/API payloads consumed by the workbench JavaScript.

All storage is temporary, the business handler/runtime are mocks, and no
scheduler loop, browser, external model or live database is used.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.automation import CodexAutomationExecutor
from packages.automation.conversations import read_conversation
from tests.test_automation_chat_20261006 import DirectService, setup
from tests.test_progress_thread_ownership_20261006 import HEADERS, daily, review


def test_scheduled_api_payloads_drive_real_frontend_through_completion_chat_and_delete(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the workbench JavaScript")
    store, schedule, settings = setup(tmp_path)
    started_at = datetime.now(timezone.utc)
    followup_at = started_at + timedelta(minutes=2)
    calls, snapshots = [], {}

    class Service(DirectService):
        followup = False
        turn_calls = 0

        async def thread_start(self):
            self.created.append("scheduled-followup-thread")
            return {"id": self.created[-1]}

        async def thread_list(self, **_kwargs):
            rows = [{"id": "review-chat", "preview": "复核任务", "updatedAt": started_at.timestamp()}]
            if self.created:
                rows.insert(0, {"id": self.created[0], "preview": "定时任务后续聊天",
                                "updatedAt": followup_at.timestamp()})
            return {"data": rows, "next_cursor": None}

        async def thread_read(self, thread_id, **_kwargs):
            if thread_id == "review-chat":
                return {"id": thread_id, "turns": [{"id": "review-turn", "items": [
                    {"type": "agentMessage", "text": "复核任务结果"}]}]}
            return {"id": thread_id, "updatedAt": followup_at.isoformat() if self.followup else started_at.timestamp(),
                    "turns": [{"id": "followup-turn", "createdAt": followup_at.timestamp(), "items": [
                        {"type": "userMessage", "text": "解释本次结果"},
                        {"type": "agentMessage", "text": "追问回答：本次新增岗位已保存"}]}] if self.followup else None}

        async def turn_start(self, thread_id, prompt):
            assert thread_id == self.created[0] and "解释本次结果" in prompt
            self.turn_calls += 1
            self.followup = True
            return {"id": "followup-turn"}

        async def thread_delete(self, thread_id):
            assert thread_id == self.created[0]
            # Deliberately keep a stale runtime listing to exercise tombstones.

    service = Service()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    client = TestClient(main.app, base_url="http://localhost")
    task = store.claim_due()

    def snapshot():
        direct_id = store.execution(task.execution_id).thread_id
        direct = client.get(f"/api/codex/threads/{direct_id}")
        listed = client.get("/api/codex/threads?limit=20")
        progress = client.get("/api/local-ui/tasks/progress", headers=HEADERS)
        assert direct.status_code == listed.status_code == progress.status_code == 200
        return {"direct": direct.json(), "list": listed.json(), "progress": progress.json()}

    def handler(context):
        calls.append(context)
        daily(store.storage, context.run_id, thread=context.metadata["thread_id"], turn=None)
        snapshots["review_run"] = review(store.storage)
        snapshots["running"] = snapshot()
        return {"status": "completed", "company_total": 3}

    try:
        result = asyncio.run(CodexAutomationExecutor(service, store, settings=settings,
            task_handlers={"daily_recruitment_intelligence": handler})(task))
        store.complete(task.execution_id, status=result.status, result_summary=result.summary,
                       result_details=result.details, thread_id=result.thread_id,
                       now=started_at + timedelta(minutes=1))
        snapshots["completed"] = snapshot()
        assert not service.created
        followup = client.post(f"/api/codex/threads/{result.thread_id}/turns",
                               json={"text": "解释本次结果"})
        assert followup.status_code == 200
        snapshots["followup"] = snapshot()
        followup_id = followup.json()["thread_id"]
        assert followup_id != result.thread_id
        snapshots["followup_history"] = client.get(f"/api/codex/threads/{followup_id}").json()
        assert len(calls) == service.turn_calls == 1
        receipt = client.delete(f"/api/codex/threads/{result.thread_id}")
        assert receipt.status_code == 200
        store.complete(task.execution_id, status=result.status, result_summary=result.summary,
                       thread_id=result.thread_id, now=followup_at + timedelta(minutes=1))
        assert read_conversation(store.storage, result.thread_id) is None
        snapshots["deleted"] = {"receipt": receipt.json(),
            "list": client.get("/api/codex/threads?limit=20").json(),
            "progress": client.get("/api/local-ui/tasks/progress", headers=HEADERS).json()}
        assert result.thread_id not in {row["id"] for row in snapshots["deleted"]["list"]["data"]}
        snapshots.update(direct_thread=result.thread_id, direct_run=task.execution_id,
                         followup_thread=followup_id,
                         review_history=client.get("/api/codex/threads/review-chat").json())
        root = Path(__file__).resolve().parents[1]
        response = subprocess.run([node, str(root / "tests/frontend/automation_payload_contract.cjs")],
            input=json.dumps(snapshots, ensure_ascii=False), text=True, encoding="utf-8",
            capture_output=True, cwd=root, timeout=30, check=False)
        assert response.returncode == 0, response.stdout + response.stderr
        report = json.loads(response.stdout)
        assert report["isolated_progress"] and report["model_turns_started"] == 0
        assert report["states"] == ["running", "completed", "followup", "deleted"]
    finally:
        store.storage.engine.dispose()
