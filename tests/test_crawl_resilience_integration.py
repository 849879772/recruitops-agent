"""Synthetic end-to-end contracts for partial source scopes; no live sites."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import packages.scheduler.runtime as runtime
from apps.api.daily_progress import latest_daily_progress
from packages.config import Settings
from packages.discovery.company_registry import CompanySourceRegistry
from packages.orchestration import DailyRecruitmentSync, DailySyncStage, DailySyncStatus
from packages.pipeline import load_companies
from packages.scheduler import TaskContext, TaskType
from packages.storage import AgentStateStore, Storage


def test_partial_sources_continue_crawl_but_never_run_offline_reconciliation():
    calls = []
    result = DailyRecruitmentSync(
        discovery=lambda: {"complete": False, "partial": True, "usable": True},
        reconcile=lambda _source: calls.append("reconcile") or {},
        crawl=lambda _dry: calls.append("crawl") or {"written": True},
        offline_reconcile=lambda *_args: calls.append("offline") or {},
        report=lambda *_args: calls.append("report") or {},
    ).run()
    assert result.status is DailySyncStatus.DEGRADED
    assert calls == ["reconcile", "crawl", "report"]
    assert result.pipeline["written"] is True
    assert result.warnings
    assert any(event.stage is DailySyncStage.DISCOVERY and event.status.value == "partial"
               for event in result.stages)


def test_resumed_partial_scope_is_not_mistaken_for_complete_discovery():
    calls = []
    result = DailyRecruitmentSync(
        crawl=lambda _dry: {"source_partial": True, "written": True},
        offline_reconcile=lambda *_args: calls.append("offline"),
    ).run()
    assert result.status is DailySyncStatus.DEGRADED
    assert not calls


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("failure_stage", [None, "checkpoint", "pipeline"])
def test_scheduler_uses_only_valid_partial_sources_and_preserves_partial_on_resume(
    tmp_path: Path, monkeypatch, resume: bool, failure_stage,
):
    config = tmp_path / "config"
    config.mkdir()
    (config / "companies.yaml").write_text("companies: []\n", encoding="utf-8")
    (config / "candidate_profile.yaml").write_text("profile: {}\n", encoding="utf-8")
    database_url = f"sqlite:///{tmp_path / 'fixture.db'}"
    storage = Storage.from_url(database_url, initialize=True)
    registry = CompanySourceRegistry(storage)
    source = registry.upsert_source(
        source="offerbiu", source_record_id="fixture-source", company_name="Fixture Co",
        source_url="https://offerbiu.com/companies/", entry_url="https://fixture.zhiye.com/campus/jobs",
    )
    registry.upsert_source(
        source="offerbiu", source_record_id="old-source", company_name="Old Co",
        source_url="https://offerbiu.com/companies/", entry_url="https://old.zhiye.com/campus/jobs",
    )
    refresh_calls, pipeline_args, scopes = [], [], []

    class Refresh:
        def __init__(self, _registry, *, scope=None):
            self.last_registered_ids = (source["id"],)

        def refresh(self, *, checkpoint_path=None, **_kwargs):
            refresh_calls.append(checkpoint_path)
            return {"complete": False, "partial": True, "usable": True, "applied": True,
                    "stop_reason": "http_503", "registered_entries": 1, "records_seen": 1}

    class Pipeline:
        def __init__(self, **kwargs):
            pipeline_args.append(kwargs)
            if failure_stage == "pipeline" and len(pipeline_args) > 1:
                raise RuntimeError("fixture pipeline setup failed")
            self.companies_path = kwargs["companies_path"]

        def run(self, **_kwargs):
            scopes.append([company.name for company in load_companies(self.companies_path)])
            return SimpleNamespace(to_dict=lambda: {"selected_companies": 1, "companies": [], "written": False})

    monkeypatch.setattr(runtime, "OfferBiuRefreshService", Refresh)
    monkeypatch.setattr(runtime, "DailyRecruitmentPipeline", Pipeline)
    if failure_stage == "checkpoint":
        def fail_checkpoint(*_args):
            raise OSError("fixture checkpoint write failed")
        monkeypatch.setattr(runtime, "_initialize_empty_company_checkpoint", fail_checkpoint)
    def forbidden_offline(*_args, **_kwargs):
        pytest.fail("partial scope must not drive offline reconciliation")
    monkeypatch.setattr(runtime, "reconcile_offline_jobs", forbidden_offline)
    settings = Settings(agent_root=tmp_path, database_url=database_url,
                        discovery_enabled=True, offline_reconciliation_enabled=True,
                        llm_enabled=False, browser_max_concurrency=1 if resume else 2)
    handler = runtime.build_runtime_task_handlers(settings=settings)[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value]
    def execute(run_id, details):
        return handler(TaskContext(
            task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value, task_label="fixture",
            scheduled_for=datetime.now(timezone.utc), run_id=run_id, attempt=1,
            write_enabled=True, metadata={"details": details},
        ))
    result = execute("partial-first", {"mode": "crawl_only"})
    if failure_stage:
        assert result["status"] == "failed"
        assert not scopes  # Especially never execute the initial legacy-config pipeline.
        assert "refusing legacy companies fallback" in result["error"]
        return
    if resume:
        result = execute("partial-resume", {"mode": "resume", "resume_run_id": "partial-first"})
    assert result["status"] == "partial"
    assert result["source_partial"] is True
    assert result["warnings"]
    assert result["daily_sync"]["offline_reconciliation"] is None
    assert all(scope == ["Fixture Co"] for scope in scopes)
    assert len(refresh_calls) == 1
    assert refresh_calls[0].parent == tmp_path / ".data" / "runtime"
    assert all(args["max_concurrency"] == 10 and args["detail_max_concurrency"] == 10
               and args["match_max_concurrency"] == 6 for args in pipeline_args)
    assert all(args["browser_max_concurrency"] == (1 if resume else 2) for args in pipeline_args)
    assert registry.get_source(source["id"]) is not None
    current = latest_daily_progress(storage)["run"]
    assert current["status"] == "partial"
    state = AgentStateStore(storage).get_task_state("partial-resume" if resume else "partial-first")
    assert state["metadata"]["source_coverage"]["partial"] is True


def test_discovery_progress_is_indeterminate_until_scope_confirmed(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'progress.db'}", initialize=True)
    AgentStateStore(storage).save_task_state("discover", {
        "current_step": "discovery", "progress": {"stage": "discovery", "pages_fetched": 4,
            "pages_total": 30, "records_seen": 200, "total_confirmed": False},
    }, ensure_task_run=True)
    assert latest_daily_progress(storage)["run"]["progress"]["total_confirmed"] is False
