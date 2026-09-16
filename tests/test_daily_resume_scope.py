from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import packages.scheduler.runtime as scheduler_runtime
from packages.config import Settings
from packages.discovery.company_registry import CompanySourceRegistry
from packages.domain.models import Company, Job, RecruitmentBatch
from packages.pipeline import CrawlResult, DailyRecruitmentPipeline
from packages.scheduler import TaskContext, TaskType
from packages.storage import Storage
from packages.storage.sync import (
    AgentStateStore,
    upsert_company_snapshot,
    upsert_job_snapshot,
)


UTC = timezone.utc


def _settings(tmp_path: Path, *, discovery_enabled: bool = True) -> Settings:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "candidate_profile.yaml").write_text("profile: {}\n", encoding="utf-8")
    return Settings(
        agent_root=tmp_path,
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'agent.db').as_posix()}",
        discovery_enabled=discovery_enabled,
        offline_reconciliation_enabled=False,
        llm_enabled=False,
    )


def _context(run_id: str, details: dict[str, object]) -> TaskContext:
    return TaskContext(
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="daily",
        scheduled_for=datetime(2026, 9, 12, tzinfo=UTC),
        run_id=run_id,
        attempt=1,
        write_enabled=True,
        metadata={"details": details},
    )


def _pipeline_spy(monkeypatch, observed: list[dict[str, object]]):
    class Pipeline:
        def __init__(self, *, companies_path=None, company_ids=(), checkpoint_path=None,
                     resume_from_checkpoint=False, **_kwargs):
            self.companies_path = Path(companies_path) if companies_path else None
            self.company_ids = tuple(company_ids)
            self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
            self.resume_from_checkpoint = resume_from_checkpoint
            self.progress_callback = None
            observed.append({
                "companies_path": self.companies_path,
                "company_ids": self.company_ids,
                "checkpoint_path": self.checkpoint_path,
                "resume_from_checkpoint": resume_from_checkpoint,
            })

        def run(self, *, dry_run=False):
            if self.checkpoint_path is not None and not self.resume_from_checkpoint:
                ids = list(self.company_ids)
                self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                self.checkpoint_path.write_text(
                    json.dumps({
                        "version": 1,
                        "company_ids": ids,
                        "companies": {},
                    }),
                    encoding="utf-8",
                )
            names = []
            if self.companies_path is not None:
                from packages.pipeline import load_companies

                names = [item.name for item in load_companies(self.companies_path)]
            return SimpleNamespace(
                written=False,
                to_dict=lambda: {
                    "selected_companies": len(self.company_ids),
                    "companies": [],
                    "names": names,
                    "written": False,
                },
            )

    monkeypatch.setattr(scheduler_runtime, "DailyRecruitmentPipeline", Pipeline)


def _source(storage: Storage, source_record_id: str = "current-1") -> dict[str, object]:
    return CompanySourceRegistry(storage).upsert_source(
        source="offerbiu",
        source_record_id=source_record_id,
        company_name="Current Company",
        source_url="https://offerbiu.com/companies/",
        entry_url="https://current.zhiye.com/campus/jobs",
    )


def test_resume_persists_scope_and_does_not_expand_or_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "config" / "companies.yaml").write_text(
        yaml.safe_dump({
            "companies": [{
                "id": "legacy-only",
                "name": "Legacy Only",
                "careers_url": "https://legacy.example.com/campus",
                "crawler": "render",
                "integration_status": "connected",
            }]
        }),
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    source = _source(storage)
    refresh_calls: list[int] = []

    class Refresh:
        def __init__(self, _registry):
            self.last_registered_ids = (source["id"],)

        def refresh(self, **_kwargs):
            refresh_calls.append(1)
            return {"complete": True, "new_entries": 1, "registered_entries": 1}

    observed: list[dict[str, object]] = []
    _pipeline_spy(monkeypatch, observed)
    monkeypatch.setattr(scheduler_runtime, "OfferBiuRefreshService", Refresh)
    monkeypatch.setattr(
        scheduler_runtime,
        "build_reporting_summary",
        lambda *_args, **_kwargs: {},
    )
    handlers = scheduler_runtime.build_runtime_task_handlers(settings=settings)

    first = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
        _context("original-run", {"mode": "full"})
    )
    assert first["status"] == "completed"
    assert len(refresh_calls) == 1
    first_state = AgentStateStore(storage).get_task_run("original-run")
    assert first_state is not None
    scope_ref = first_state["metadata"]["scope_ref"]
    assert Path(scope_ref).is_file()
    assert first_state["metadata"]["checkpoint_ref"]

    # Simulate process recovery while the original run was in company crawling.
    state = AgentStateStore(storage).get_task_state("original-run")
    assert state is not None
    state["current_step"] = "companies:1/2"
    state["details"]["stage"] = "companies:1/2"
    AgentStateStore(storage).save_task_state("original-run", state)

    second = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
        _context(
            "resumed-run",
            {"mode": "resume", "resume_run_id": "original-run"},
        )
    )
    assert second["effective_mode"] == "full"
    assert second["resumed_from"] == "original-run"
    assert len(refresh_calls) == 1
    resumed = observed[-1]
    assert resumed["resume_from_checkpoint"] is True
    assert resumed["companies_path"] == Path(scope_ref)
    assert "legacy-only" in set(resumed["company_ids"])
    assert len(resumed["company_ids"]) == 2

    # A resumed run may itself be resumed.  Keep the original effective mode
    # authoritative even if an adapter records the wrapper request as "resume".
    second_state = AgentStateStore(storage).get_task_state("resumed-run")
    assert second_state is not None
    second_state["metadata"]["requested_mode"] = "resume"
    second_state["details"]["requested_mode"] = "resume"
    AgentStateStore(storage).save_task_state("resumed-run", second_state)
    third = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
        _context(
            "resumed-again-run",
            {"mode": "resume", "resume_run_id": "resumed-run"},
        )
    )
    assert third["effective_mode"] == "score_only"
    assert third["resumed_from"] == "resumed-run"
    assert len(refresh_calls) == 1
    assert observed[-1]["resume_from_checkpoint"] is False


def test_crawl_only_refreshes_and_freezes_current_offerbiu_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies: []\n",
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    source = _source(storage, "crawl-only-source")
    refresh_calls: list[int] = []

    class Refresh:
        def __init__(self, _registry):
            self.last_registered_ids = (source["id"],)

        def refresh(self, **_kwargs):
            refresh_calls.append(1)
            return {"complete": True, "new_entries": 1, "registered_entries": 1}

    observed: list[dict[str, object]] = []
    _pipeline_spy(monkeypatch, observed)
    monkeypatch.setattr(scheduler_runtime, "OfferBiuRefreshService", Refresh)
    monkeypatch.setattr(
        scheduler_runtime,
        "build_reporting_summary",
        lambda *_args, **_kwargs: {},
    )
    result = scheduler_runtime.build_runtime_task_handlers(settings=settings)[
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    ](_context("crawl-only-run", {"mode": "crawl_only"}))

    assert result["status"] == "completed"
    assert len(refresh_calls) == 1
    assert observed[-1]["company_ids"]
    assert observed[-1]["resume_from_checkpoint"] is False


def test_resume_rejects_explicit_scope_override(tmp_path: Path) -> None:
    class Pipeline:
        def run(self, *, dry_run=False):
            raise AssertionError("resume scope conflict must fail before pipeline")

    handlers = scheduler_runtime.build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=SimpleNamespace(),
        daily_pipeline=Pipeline(),
    )
    with pytest.raises(ValueError, match="cannot override"):
        handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
            _context(
                "conflict-run",
                {
                    "mode": "resume",
                    "resume_run_id": "previous-run",
                    "company_ids": ["new-company"],
                },
            )
        )


def test_resume_without_persisted_scope_fails_closed(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies: []\n",
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    AgentStateStore(storage).save_task_state(
        "old-run",
        {
            "metadata": {"requested_mode": "full"},
            "details": {"stage": "companies:2/10"},
        },
        ensure_task_run=True,
    )
    monkeypatch.setattr(
        scheduler_runtime,
        "OfferBiuRefreshService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing scope must fail before refresh")
        ),
    )
    handlers = scheduler_runtime.build_runtime_task_handlers(
        settings=settings,
        daily_pipeline=SimpleNamespace(run=lambda **_kwargs: None),
    )
    with pytest.raises(ValueError, match="persisted frozen company scope"):
        handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
            _context("new-run", {"mode": "resume", "resume_run_id": "old-run"})
        )


def test_score_only_does_not_crawl_and_reuses_finite_persisted_score(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, discovery_enabled=True)
    settings.llm_enabled = True
    settings.job_analysis_enabled = True
    (tmp_path / "config" / "companies.yaml").write_text(
        yaml.safe_dump({
            "companies": [{
                "id": "frozen-company",
                "name": "Frozen Company",
                "careers_url": "https://frozen.example.com/campus",
                "crawler": "render",
                "integration_status": "connected",
            }]
        }),
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    observed_plan_args: list[tuple[str, ...]] = []

    class Pipeline:
        def __init__(self, **_kwargs):
            self.progress_callback = None

        def run(self, *, dry_run=False):
            raise AssertionError("score_only must not invoke the crawler pipeline")

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

    candidate = SimpleNamespace(job=SimpleNamespace(match_score=91.0))
    plan = SimpleNamespace(
        pending_jobs=(candidate,),
        pending_count=1,
        as_dict=lambda: {"pending_jobs": 1},
    )

    def build_plan(_storage, _profile, *, company_ids=(), **_kwargs):
        observed_plan_args.append(tuple(company_ids))
        return plan

    def resume(_storage, _profile, _service, pending_jobs, **_kwargs):
        assert tuple(pending_jobs) == ()
        return SimpleNamespace(
            planned=0,
            completed=0,
            failed=0,
            refused=0,
            processed=0,
            errors={},
            as_dict=lambda: {"planned": 0, "processed": 0},
        )

    monkeypatch.setattr(scheduler_runtime, "DailyRecruitmentPipeline", Pipeline)
    monkeypatch.setattr(scheduler_runtime, "DeepSeekClient", FakeClient)
    monkeypatch.setattr(scheduler_runtime, "MatchingService", FakeService)
    monkeypatch.setattr(scheduler_runtime, "build_analysis_resume_plan", build_plan)
    monkeypatch.setattr(scheduler_runtime, "resume_pending_analyses", resume)
    monkeypatch.setattr(
        scheduler_runtime,
        "build_reporting_summary",
        lambda *_args, **_kwargs: {},
    )

    result = scheduler_runtime.build_runtime_task_handlers(settings=settings)[
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    ](_context("score-run", {"mode": "score_only", "company_ids": ["frozen-company"]}))

    assert result["status"] == "completed"
    assert result["effective_mode"] == "score_only"
    assert result["scoring_candidates"] == 0
    assert observed_plan_args == [("frozen-company",)]
    state = AgentStateStore(storage).get_task_state("score-run")
    assert state is not None
    assert state["current_step"] == "matching"
    assert state["details"]["stage"] == "matching"


def test_unscoped_score_only_freezes_local_candidates_without_biu_or_crawl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, discovery_enabled=True)
    (tmp_path / "config" / "companies.yaml").write_text(
        yaml.safe_dump({
            "companies": [{
                "id": "local-company",
                "name": "Local Company",
                "careers_url": "https://local.example.com/campus",
                "crawler": "render",
                "integration_status": "connected",
            }]
        }),
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    company = Company(
        id="local-company",
        name="Local Company",
        integration_status="connected",
        source="fixture",
        source_ref="company:local-company",
    )
    job = Job(
        id="local-job",
        company_id=company.id,
        title="C++ Engineer",
        detail_url="https://local.example.com/jobs/1",
        jd_raw="C++ Linux responsibilities and requirements",
        cohort=2027,
        cohort_status="confirmed",
        batch=RecruitmentBatch.FORMAL,
        match_score=84,
        source="fixture",
        source_ref="job:local-job",
    )
    with storage.write_transaction() as session:
        upsert_company_snapshot(session, company)
        upsert_job_snapshot(session, job)
    observed: list[dict[str, object]] = []
    _pipeline_spy(monkeypatch, observed)
    monkeypatch.setattr(
        scheduler_runtime,
        "OfferBiuRefreshService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("score_only must not refresh BIU")
        ),
    )
    monkeypatch.setattr(
        scheduler_runtime,
        "build_reporting_summary",
        lambda *_args, **_kwargs: {},
    )

    result = scheduler_runtime.build_runtime_task_handlers(settings=settings)[
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    ](_context("unscoped-score-run", {"mode": "score_only"}))

    assert result["status"] == "completed"
    assert observed[-1]["company_ids"] == ("local-company",)
    assert observed[-1]["resume_from_checkpoint"] is False
    assert AgentStateStore(storage).get_task_run("unscoped-score-run")["metadata"]["scope_ref"]


def test_unscoped_score_only_includes_db_only_job_companies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path, discovery_enabled=True)
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies: []\n",
        encoding="utf-8",
    )
    storage = Storage.from_url(settings.database_url, initialize=True)
    company = Company(
        id="db-only-company",
        name="DB Only Company",
        integration_status="connected",
        source="fixture",
        source_ref="company:db-only-company",
    )
    job = Job(
        id="db-only-job",
        company_id=company.id,
        title="C++ Engineer",
        detail_url="https://db-only.example.com/jobs/1",
        jd_raw="C++ Linux responsibilities and requirements",
        cohort=2027,
        cohort_status="confirmed",
        batch=RecruitmentBatch.FORMAL,
        match_score=86,
        source="fixture",
        source_ref="job:db-only-job",
    )
    with storage.write_transaction() as session:
        upsert_company_snapshot(session, company)
        upsert_job_snapshot(session, job)

    observed: list[dict[str, object]] = []
    _pipeline_spy(monkeypatch, observed)
    monkeypatch.setattr(
        scheduler_runtime,
        "OfferBiuRefreshService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DB-only score_only must not refresh BIU")
        ),
    )
    monkeypatch.setattr(
        scheduler_runtime,
        "build_reporting_summary",
        lambda *_args, **_kwargs: {},
    )

    result = scheduler_runtime.build_runtime_task_handlers(settings=settings)[
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    ](_context("db-only-score-run", {"mode": "score_only"}))

    assert result["status"] == "completed"
    assert observed[-1]["company_ids"] == ("db-only-company",)
    assert observed[-1]["companies_path"].name.startswith("daily-saved-score-scope-")


def test_company_checkpoint_skips_completed_and_retries_only_failed_company(
    tmp_path: Path,
) -> None:
    companies_path = tmp_path / "companies.yaml"
    companies_path.write_text(
        yaml.safe_dump({
            "companies": [
                {
                    "id": "done",
                    "name": "Done",
                    "careers_url": "https://done.example.com/campus",
                    "crawler": "fake",
                    "integration_status": "connected",
                },
                {
                    "id": "retry",
                    "name": "Retry",
                    "careers_url": "https://retry.example.com/campus",
                    "crawler": "fake",
                    "integration_status": "connected",
                },
            ]
        }),
        encoding="utf-8",
    )
    storage = Storage.from_url(
        f"sqlite+pysqlite:///{(tmp_path / 'pipeline.db').as_posix()}",
        initialize=True,
    )
    checkpoint_path = tmp_path / "daily-checkpoint.json"
    first_calls: list[str] = []

    def first_crawl(company):
        first_calls.append(company.id)
        if company.id == "retry":
            raise RuntimeError("temporary crawler outage")
        return CrawlResult(
            jobs=[],
            source_url=company.careers_url,
            allowed_origins=["https://done.example.com"],
            pages_seen=1,
            total_pages=1,
            pagination_complete=True,
            completeness_known=True,
        )

    first = DailyRecruitmentPipeline(
        companies_path=companies_path,
        storage=storage,
        crawler=first_crawl,
        max_concurrency=1,
        checkpoint_path=checkpoint_path,
    ).run()
    assert first.failed_company_count == 1
    assert first_calls == ["done", "retry"]

    second_calls: list[str] = []

    def second_crawl(company):
        second_calls.append(company.id)
        if company.id == "done":
            raise AssertionError("completed company was recrawled")
        return CrawlResult(
            jobs=[],
            source_url=company.careers_url,
            allowed_origins=["https://retry.example.com"],
            pages_seen=1,
            total_pages=1,
            pagination_complete=True,
            completeness_known=True,
        )

    second = DailyRecruitmentPipeline(
        companies_path=companies_path,
        storage=storage,
        crawler=second_crawl,
        max_concurrency=1,
        checkpoint_path=checkpoint_path,
        resume_from_checkpoint=True,
    ).run()
    assert second_calls == ["retry"]
    assert second.resumed_company_ids == ("done",)
    assert second.retried_company_ids == ("retry",)
