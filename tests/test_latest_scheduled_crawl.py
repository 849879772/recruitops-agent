import asyncio
from dataclasses import replace
import json
import time

from packages.automation.latest_report import summarize, report_path
from packages.config import Settings
from apps.api.automation import CodexAutomationExecutor
from tests.test_automation_executor import _task


def fixture_result():
    return {"status": "completed", "sync_status": "succeeded", "daily_sync": {
        "discovery": {"new_companies": 2, "new_entries": 3, "excluded_unusable": 5},
        "pipeline": {"new": 8, "scored": 6, "scoring_failed": 2, "companies": [
            {"status": "complete", "raw_job_count": 10, "detail_failure_count": 1},
            {"status": "complete", "raw_job_count": 20}, {"status": "failed"},
        ]}}}


def test_report_counts_partial_details_separately():
    report = summarize(fixture_result(), "one")
    assert report["new_companies"] == 2
    assert report["new_entries"] == 3
    assert report["list_jobs"] == 30
    assert report["complete_companies"] == report["partial_companies"] == report["failed_companies"] == 1
    assert report["new_jobs"] == 8 and report["scored_jobs"] == 6
    assert report["status"] == "partial"


def test_scheduled_pipeline_ignores_chat_timeout_and_preserves_scope(tmp_path, monkeypatch):
    from packages import config
    from packages.scheduler import runtime
    settings = Settings(agent_root=tmp_path, write_enabled=True)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    def handler(context):
        assert context.write_enabled
        assert context.metadata["details"]["company_ids"] == ["example"]
        time.sleep(0.03)
        return fixture_result()
    monkeypatch.setattr(runtime, "build_runtime_task_handlers", lambda **kwargs: {"daily_recruitment_intelligence": handler})
    executor = CodexAutomationExecutor(None, None)
    executor.timeout_seconds = 0.001
    task = replace(_task(), task_id="daily_recruitment_intelligence", target_kind="company", target_id="example")
    assert asyncio.run(executor(task)).status == "succeeded"
    assert json.loads(report_path(settings).read_text(encoding="utf-8"))["execution_id"] == task.execution_id
    asyncio.run(executor(replace(task, execution_id="second")))
    assert json.loads(report_path(settings).read_text(encoding="utf-8"))["execution_id"] == "second"
    assert len(list(report_path(settings).parent.glob("latest-*.json"))) == 1
