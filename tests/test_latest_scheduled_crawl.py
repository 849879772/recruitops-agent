import asyncio
from dataclasses import replace
from datetime import datetime, time as wall_time, timezone
import json
import time
from types import SimpleNamespace

import pytest

from packages.automation import AutomationStore, LocalAutomationWorker
from packages.automation.latest_report import summarize, report_path
from packages.config import Settings
from packages.storage import Storage
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


@pytest.mark.parametrize("payload, status, error", [
    ({"status": "completed", "message": "完成"}, "succeeded", None),
    (fixture_result(), "partial", None),
    ({"status": "completed", "sync_status": "degraded"}, "partial", None),
    ({"status": "configuration_required", "missing": ["title_keywords"],
      "message": "请先配置岗位标题关键词和行业范围；助理聊天不受此限制。"},
     "failed", "请先配置岗位标题关键词和行业范围；助理聊天不受此限制。\n缺少配置项：title_keywords"),
    ({"status": "failed", "error": "source unavailable", "message": "运行未完成"},
     "failed", "source unavailable"),
    ({"status": "failed", "message": "browser unavailable"}, "failed", "browser unavailable"),
    ({"status": "failed", "error": "wrapper error", "message": "运行未完成",
      "daily_sync": {"error": "original pipeline error"}}, "failed", "original pipeline error"),
    ({"status": "configuration_required", "missing": ["title_keywords", "industry_groups"]},
     "failed", "全量任务执行失败（状态：configuration_required）\n缺少配置项：title_keywords、industry_groups"),
    ({"status": "storage_unavailable"}, "failed", "全量任务执行失败（状态：storage_unavailable）"),
    ({}, "failed", "全量任务执行失败（状态：unknown）"),
    ({"status": "completed", "sync_status": "degraded",
      "daily_sync": {"error": "discovery failed; using configured companies"}},
     "partial", "discovery failed; using configured companies"),
])
def test_daily_result_reason_survives_report_and_execution(tmp_path, payload, status, error):
    settings = Settings(agent_root=tmp_path, write_enabled=True)
    store = AutomationStore(Storage.from_url(f"sqlite:///{tmp_path / 'automation.db'}", initialize=True))
    schedule = store.upsert_daily(
        task_id="daily_recruitment_intelligence", task_label="test daily",
        start_time=wall_time(16, 25),
        now=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    calls = []

    def handler(context):
        calls.append(context)
        return payload

    # No chat service or production handler is available in this isolated run.
    executor = CodexAutomationExecutor(
        None, store, settings=settings,
        task_handlers={"daily_recruitment_intelligence": handler},
    )
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())

    report = json.loads(report_path(settings).read_text(encoding="utf-8"))
    execution = store.executions(schedule.id)[0]
    persisted_schedule = store.list()[0]
    assert len(calls) == 1
    assert report["execution_id"] == execution.id
    assert report["status"] == status
    assert report["source_status"] == payload.get("status")
    assert report["message"] == payload.get("message")
    assert report["missing"] == payload.get("missing", [])
    assert report["error"] == execution.error == persisted_schedule.last_error == error
    assert execution.status == persisted_schedule.last_status == (
        "failed" if status == "failed" else "succeeded"
    )
    if status == "failed":
        assert error in execution.result_summary
    else:
        assert execution.result_summary == (
            "全量任务已结束；部分公司或评分未完成" if status == "partial" else "全量任务已完成"
        )
    assert execution.thread_id is None and execution.turn_id is None


@pytest.mark.parametrize("during_setup", [False, True])
def test_daily_exception_keeps_original_reason_as_failed_result(tmp_path, monkeypatch, during_setup):
    settings = Settings(agent_root=tmp_path, write_enabled=True)
    failure = RuntimeError("source refresh connection refused")

    def fail(*args):
        raise failure

    executor = CodexAutomationExecutor(
        None, None, settings=settings,
        task_handlers={"daily_recruitment_intelligence": fail},
    )
    if during_setup:
        monkeypatch.setattr(executor, "_runtime_handlers", fail)
    task = replace(_task(), task_id="daily_recruitment_intelligence", target_kind="all", target_id=None)
    result = asyncio.run(executor(task))

    assert result.status == "failed"
    report = json.loads(report_path(settings).read_text(encoding="utf-8"))
    assert report["execution_id"] == task.execution_id
    assert report["status"] == "failed"
    assert "RuntimeError: source refresh connection refused" in report["error"]
    assert report["error"] == result.error
    assert result.error in result.summary


def test_summarize_redacts_error_and_message_without_settings():
    diagnostic = "provider authentication rejected sk-0123456789abcdef01234567 for fixture@example.test"
    report = summarize({"status": "failed", "error": diagnostic, "message": diagnostic}, "test")
    for field in ("error", "message"):
        assert "provider authentication rejected" in report[field]
        assert "sk-0123456789abcdef01234567" not in report[field]
        assert "fixture@example.test" not in report[field]
        assert "[REDACTED:" in report[field]


@pytest.mark.parametrize("source", [
    "exception", "setup_exception", "error", "message", "nested_error", "partial", "success",
])
def test_daily_sensitive_diagnostics_are_redacted_before_persistence(tmp_path, monkeypatch, source):
    # Deliberately nonstandard secrets exercise exact-value replacement before
    # generic email/token redaction can alter a credential-bearing DSN.
    settings = SimpleNamespace(
        agent_root=tmp_path, write_enabled=True,
        llm_api_key="fixture-provider-credential",
        mail_imap_password="fixture-mail-passphrase",
        database_url="postgresql://fixture:fixture-db-pass@db.example.test/recruitment",
    )
    secrets = [settings.llm_api_key, settings.mail_imap_password, settings.database_url,
               "sk-0123456789abcdef01234567", "fixture@example.test"]
    diagnostic = "provider authentication rejected; " + "; ".join(secrets)
    secrets.append("fixture-db-pass")
    store = AutomationStore(Storage.from_url(f"sqlite:///{tmp_path / 'automation.db'}", initialize=True))
    schedule = store.upsert_daily(
        task_id="daily_recruitment_intelligence", task_label="test daily",
        start_time=wall_time(16, 25), now=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    def handler(context):
        if source in {"exception", "setup_exception"}:
            raise RuntimeError(diagnostic)
        payload = {"status": "failed", "message": diagnostic}
        if source == "error":
            payload["error"] = diagnostic
        if source in {"nested_error", "partial"}:
            payload["daily_sync"] = {"error": diagnostic}
        if source in {"partial", "success"}:
            payload["status"] = "completed"
            payload["sync_status"] = "degraded" if source == "partial" else "succeeded"
        return payload

    executor = CodexAutomationExecutor(
        None, store, settings=settings,
        task_handlers={"daily_recruitment_intelligence": handler},
    )
    if source == "setup_exception":
        monkeypatch.setattr(executor, "_runtime_handlers", handler)
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())

    report_text = report_path(settings).read_text(encoding="utf-8")
    report = json.loads(report_text)
    execution = store.executions(schedule.id)[0]
    persisted_text = report_text + (execution.error or "") + (execution.result_summary or "")
    persisted_text += store.list()[0].last_error or ""
    assert all(secret not in persisted_text for secret in secrets)
    assert report["status"] == (
        "succeeded" if source == "success" else "partial" if source == "partial" else "failed"
    )
    assert execution.status == ("succeeded" if source in {"success", "partial"} else "failed")
    assert report["error"] == execution.error == store.list()[0].last_error
    assert "provider authentication rejected" in (report["error"] or report["message"])
    assert "[REDACTED:" in persisted_text
    if source != "success":
        assert "provider authentication rejected" in execution.error
    if source in {"exception", "setup_exception"}:
        assert "RuntimeError" in execution.error


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
