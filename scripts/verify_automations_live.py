"""Bounded live acceptance: real timer claims, agent turns, and persisted outcomes."""
import asyncio
import argparse
import json
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from apps.api.automation import CodexAutomationExecutor
from apps.api.codex_bff import get_codex_bff_service
from packages.automation import AutomationStore, LocalAutomationWorker
from packages.config import get_settings
from packages.storage import AutomationSchedule, Storage


async def main():
    settings = get_settings()
    storage = Storage.from_url(settings.database_url)
    store = AutomationStore(storage)
    assert not store.list(active_only=True), "Disable prior schedules explicitly before acceptance"
    service = get_codex_bff_service()
    tasks = [
        ("crawler_health", "all", None),
        ("recruitment_mailbox", "all", None),
        ("application_progress", "application", "24"),
        ("daily_recruitment_intelligence", "company", "config-12"),
    ]
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default="")
    parser.add_argument("--application", default="24")
    args = parser.parse_args()
    tasks = [(name, kind, args.application if name == "application_progress" else target)
             for name, kind, target in tasks if not args.tasks or name in args.tasks.split(",")]
    ids, results = [], []
    path = Path("/app/.data/automation-acceptance-20260913" + ("-retest" if args.tasks else "") + ".json")
    executor = CodexAutomationExecutor(service, store, timeout_seconds=300)

    async def execute(claim):
        print("START", claim.task_id, flush=True)
        try:
            result = await executor(claim)
            results.append({"task_id": claim.task_id, "execution_id": claim.execution_id, **asdict(result)})
            path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print("RESULT", claim.task_id, result.status, (result.summary or "")[:1600], flush=True)
            return result
        finally:
            store.disable(claim.schedule_id)

    worker = LocalAutomationWorker(store, execute, poll_seconds=0.5)
    try:
        await service.start()
        for index, (task_id, kind, target) in enumerate(tasks):
            row = store.upsert_daily(task_id=task_id, task_label="定时验收：" + task_id,
                                     start_time=time(23, 59), target_kind=kind, target_id=target)
            ids.append(row.id)
            with storage.write_transaction() as session:
                session.get(AutomationSchedule, row.id).next_run_at = datetime.now(timezone.utc) + timedelta(seconds=2+index)
        for _ in tasks:
            while not await worker.run_once():
                await asyncio.sleep(0.5)
        assert not await worker.run_once(), "An occurrence was claimed twice"
        print("COMPLETE", len(results), "active", len(store.list(active_only=True)), flush=True)
    finally:
        for schedule_id in ids:
            store.disable(schedule_id)
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
