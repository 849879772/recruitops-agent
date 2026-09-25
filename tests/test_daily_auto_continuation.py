"""Short deterministic budgets exercise the six-hour continuation boundary."""
from datetime import datetime, time, timezone
from threading import Event
from pathlib import Path

import pytest

from packages.scheduler import DailySchedule, LocalTaskScheduler, RunStatus, TaskDefinition


def scheduler(tmp_path):
    task = TaskDefinition(task_id="daily", label="fixture", schedule=DailySchedule(time(8)),
                          timeout_seconds=0.02, max_retries=0, cooperative_timeout=True,
                          auto_continue_on_timeout=True)
    return LocalTaskScheduler(tasks={"daily": task}, lock_path=tmp_path / "scheduler.lock")


def run(engine, handler, **kwargs):
    now = datetime.now(timezone.utc)
    return engine.run("daily", handler, now=now, scheduled_for=now, run_id="stable-run",
                      metadata={"mode": "full", "company_ids": ["frozen"]}, **kwargs)


def test_timeout_continues_under_one_lock_and_identity(tmp_path):
    engine = scheduler(tmp_path)
    contexts = []
    def handler(context):
        contexts.append(context)
        assert run(engine, lambda _: pytest.fail("overlapping handler")).status is RunStatus.SKIPPED_LOCKED
        if context.segment < 3:
            assert context.stop_requested.wait(2)
            assert context.budget_expired.is_set()
            return {"status": "paused", "continuation": {"progress": str(context.segment)}}
        assert context.metadata["details"]["mode"] == "resume"
        assert context.metadata["details"]["resume_run_id"] == "stable-run"
        assert context.metadata["details"]["company_ids"] == []
        assert not context.stop_requested.is_set()
        return {"status": "completed"}
    result = run(engine, handler)
    assert result.status is RunStatus.SUCCESS and result.attempts == 1
    assert result.value["execution_segments"] == 3
    assert [context.segment for context in contexts] == [1, 2, 3]
    assert {context.run_id for context in contexts} == {"stable-run"}
    assert run(engine, lambda _: {"status": "completed"}).status is RunStatus.SUCCESS


@pytest.mark.parametrize("reason", ["explicit_pause", "missing_checkpoint", "failure", "stalled", "external_cancel"])
def test_unsafe_continuations_stop_instead_of_looping(tmp_path, reason):
    engine, external, calls = scheduler(tmp_path), Event(), []
    def handler(context):
        calls.append(context.segment)
        if reason == "explicit_pause":
            context.stop_requested.set()
        else:
            assert context.stop_requested.wait(2)
        if reason == "external_cancel":
            external.set()
        return {"status": "failed" if reason == "failure" else "paused",
                **({} if reason == "missing_checkpoint" else {"continuation": {"progress": "unchanged"}})}
    result = run(engine, handler, stop_requested=external)
    assert result.status is (RunStatus.FAILED if reason == "failure" else RunStatus.PAUSED)
    assert calls == ([1, 2] if reason == "stalled" else [1])
    assert "continuation" not in result.value
    if reason == "stalled":
        assert result.value["continuation_blocked"] == "no_progress"
    if reason == "external_cancel":
        assert external.is_set()  # Never clear durable/user cancellation to resume.


def test_manual_stop_is_forwarded_without_waiting_for_budget(tmp_path):
    engine, external = scheduler(tmp_path), Event()
    def handler(context):
        external.set()
        assert context.stop_requested.wait(2)
        assert not context.budget_expired.is_set()
        return {"status": "paused", "continuation": {"progress": "saved"}}
    assert run(engine, handler, stop_requested=external).status is RunStatus.PAUSED


def test_dry_run_never_calls_continuation_or_handler(tmp_path):
    assert run(scheduler(tmp_path), lambda _: pytest.fail("dry run executed"), dry_run=True).status is RunStatus.DRY_RUN


def test_only_daily_task_enables_automatic_continuation():
    from packages.scheduler import TaskType, default_task_definitions
    enabled = [task.task_id for task in default_task_definitions().values() if task.auto_continue_on_timeout]
    assert enabled == [TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value]


def test_automatic_continuation_requires_cooperative_worker():
    with pytest.raises(ValueError, match="requires cooperative timeout"):
        TaskDefinition(task_id="unsafe", label="fixture", schedule=DailySchedule(time(8)),
                       auto_continue_on_timeout=True)


@pytest.mark.parametrize("phase,stop_at_boundary", [("crawl", False), ("crawl", True), ("score", False), ("invalid_checkpoint", False), ("bounded", False)])
def test_real_runtime_auto_resumes_saved_companies_without_rediscovery(tmp_path, monkeypatch, phase, stop_at_boundary):
    from dataclasses import replace
    import json
    import yaml
    from types import SimpleNamespace
    import packages.scheduler.runtime as runtime
    from packages.pipeline import CrawlResult, DailyRecruitmentPipeline
    from packages.scheduler import TaskType, default_task_definitions
    from packages.storage import Storage
    from packages.storage.sync import AgentStateStore
    from packages.tools.task_runtime_control import register_daily_task, request_daily_control
    from tests.test_daily_resume_scope import _settings

    settings = _settings(tmp_path, discovery_enabled=False)
    settings = settings.model_copy(update={"crawl_max_concurrency": 1, "llm_enabled": phase == "score",
                                           "job_analysis_enabled": phase == "score", "llm_api_key": "fixture"})
    (tmp_path / "config/companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"id": key, "name": key, "careers_url": f"https://fixture.example/{key}",
         "crawler": "fixture", "integration_status": "connected"} for key in ("a", "b", "c")
    ]}), encoding="utf-8")
    storage = Storage.from_url(settings.database_url, initialize=True)
    task_id = TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    register_daily_task(storage, "stable-run", task_id)
    calls = []
    scored = []
    if phase == "invalid_checkpoint":
        def invalid(*args, **kwargs):
            raise ValueError("synthetic invalid frozen scope")
        monkeypatch.setattr(runtime, "_load_frozen_resume", invalid)
    if phase == "score":
        def plan(*args, **kwargs):
            return SimpleNamespace(completed_jobs=len(scored), eligible_jobs=3,
                pending_jobs=[SimpleNamespace(job=SimpleNamespace(match_score=None)) for _ in range(3 - len(scored))])
        def score(_storage, _profile, _service, pending, *, progress, stop_requested, **kwargs):
            count = len(pending)
            scored.append(count)
            if len(scored) < 3:
                assert stop_requested.wait(3)
            value = SimpleNamespace(planned=count, completed=1, processed=1, failed=0, refused=0,
                errors={}, stopped_reason="time_budget_reached" if len(scored) < 3 else None,
                as_dict=lambda: {"processed": 1})
            progress(value)
            return value
        monkeypatch.setattr(runtime, "build_analysis_resume_plan", plan)
        monkeypatch.setattr(runtime, "resume_pending_analyses", score)

    class Pipeline(DailyRecruitmentPipeline):
        def __init__(self, **kwargs):
            def crawl(company):
                calls.append(company.id)
                if company.id == "a" and phase != "score":
                    assert calls == ["a"]
                    assert self.stop_requested.wait(3)
                    if stop_at_boundary:
                        assert request_daily_control(storage, "stable-run", "cancel")["success"]
                return CrawlResult(jobs=[], source_url=company.careers_url,
                    allowed_origins=["https://fixture.example"], pages_seen=1, total_pages=1,
                    pagination_complete=True, completeness_known=True)
            super().__init__(crawler=crawl, **kwargs)
    monkeypatch.setattr(runtime, "DailyRecruitmentPipeline", Pipeline)
    monkeypatch.setattr(runtime, "build_reporting_summary", lambda *args, **kwargs: {})
    handlers = runtime.build_runtime_task_handlers(settings=settings)
    # Leave room for Windows/SQLite checkpoint IO before exercising the
    # cooperative stop; the fixture crawler still waits 3s for that stop.
    task = replace(default_task_definitions()[task_id], timeout_seconds=1.8)
    engine = LocalTaskScheduler(tasks={task_id: task}, lock_path=tmp_path / "scheduler.lock")
    now = datetime.now(timezone.utc)
    metadata = {"mode": "full", "company_ids": ["a", "b", "c"]}
    if phase == "bounded":
        metadata["company_batch_limit"] = 1
    result = engine.run(task_id, handlers[task_id], now=now, scheduled_for=now,
                        run_id="stable-run", metadata=metadata)
    stopped = stop_at_boundary or phase in {"invalid_checkpoint", "bounded"}
    assert result.status is (RunStatus.PAUSED if stopped else RunStatus.SUCCESS), result
    assert calls == (["a"] if stopped else ["a", "b", "c"])
    persisted = AgentStateStore(storage).get_task_run("stable-run")
    checkpoint = runtime._state_value(runtime._state_sections(persisted), "checkpoint_ref")
    saved = json.loads(Path(checkpoint).read_text(encoding="utf-8"))
    assert saved["companies"]["a"]["status"] == "complete"
    if phase == "invalid_checkpoint":
        assert result.value["continuation_blocked"] == "checkpoint_unavailable"
    if not stopped:
        assert len(saved["companies"]) == 3
        assert persisted["state"]["automatic_segment"] == (3 if phase == "score" else 2)
        if phase == "score":
            assert scored == [3, 2, 1]
            assert result.value["effective_mode"] == "score_only"
