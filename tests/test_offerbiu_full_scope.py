from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
import yaml

import packages.scheduler.runtime as scheduler_runtime
from packages.config import Settings
from packages.scheduler import TaskContext, TaskType
from packages.discovery.company_registry import CompanySourceRegistry
from packages.pipeline import load_companies
from packages.scheduler.runtime import _prepare_full_offerbiu_scope
from packages.storage import Storage


def test_full_offerbiu_scope_uses_only_current_sources(
    tmp_path: Path,
) -> None:
    companies_path = tmp_path / "companies.yaml"
    companies_path.write_text(
        yaml.safe_dump({
            "companies": [
                {
                    "id": "legacy-duplicate",
                    "name": "BIU Company",
                    "careers_url": "https://old.example.com/campus",
                    "crawler": "render",
                    "integration_status": "connected",
                },
                {
                    "id": "legacy-only",
                    "name": "Legacy Only",
                    "careers_url": "https://legacy.example.com/campus",
                    "crawler": "render",
                    "integration_status": "connected",
                },
            ]
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    current = [
        registry.upsert_source(
            source="offerbiu",
            source_record_id="current-1",
            company_name="BIU Company",
            source_url="https://offerbiu.com/companies/",
            entry_url="https://one.zhiye.com/campus/jobs",
        ),
        registry.upsert_source(
            source="offerbiu",
            source_record_id="current-2",
            company_name="New Company",
            source_url="https://offerbiu.com/companies/",
            entry_url="https://two.zhiye.com/campus/jobs",
        ),
        registry.upsert_source(
            source="offerbiu",
            source_record_id="current-form",
            company_name="Form Company",
            source_url="https://offerbiu.com/companies/",
            entry_url="https://www.wjx.cn/vm/example.aspx",
        ),
    ]
    settings = SimpleNamespace(
        companies_config=companies_path,
        agent_root=tmp_path,
    )

    scope_path, offerbiu_count = _prepare_full_offerbiu_scope(
        settings,
        storage,
        "run-1",
        tuple(row["id"] for row in current),
    )

    companies = load_companies(scope_path)
    assert offerbiu_count == 2
    assert [company.name for company in companies] == [
        "BIU Company",
        "New Company",
    ]
    assert companies[0].careers_url == "https://one.zhiye.com/campus/jobs"
    assert companies[0].extra["discovery_source"] == "offerbiu"


def test_full_offerbiu_scope_rejects_zero_matches_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    companies_path = tmp_path / "companies.yaml"
    companies_path.write_text(
        "companies:\n  - id: legacy-only\n    name: Legacy Only\n"
        "    careers_url: https://legacy.example.com/campus\n"
        "    crawler: render\n    integration_status: connected\n",
        encoding="utf-8",
    )
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    settings = SimpleNamespace(companies_config=companies_path, agent_root=tmp_path)

    try:
        _prepare_full_offerbiu_scope(settings, storage, "empty-run", ())
    except ValueError as exc:
        assert "refusing legacy companies fallback" in str(exc)
    else:
        raise AssertionError("an empty BIU snapshot must not use legacy companies")


def test_unscoped_full_run_uses_complete_offerbiu_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "companies.yaml").write_text("companies: []\n", encoding="utf-8")
    (config_dir / "candidate_profile.yaml").write_text("profile: {}\n", encoding="utf-8")
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'agent.db').as_posix()}"
    storage = Storage.from_url(database_url, initialize=True)
    source = CompanySourceRegistry(storage).upsert_source(
        source="offerbiu",
        source_record_id="current-company",
        company_name="Current Company",
        source_url="https://offerbiu.com/companies/",
        entry_url="https://current.zhiye.com/campus/jobs",
    )
    observed: list[list[str]] = []

    class RefreshService:
        def __init__(self, _registry):
            self.last_registered_ids = (source["id"],)

        def refresh(self, **_kwargs):
            return {
                "complete": True,
                "stop_reason": "complete",
                "pages_fetched": 1,
                "records_seen": 1,
                "companies_seen": 1,
                "applied": True,
                "registered_entries": 1,
                "new_entries": 0,
                "linked_existing_entries": 0,
                "excluded_unusable": 0,
                "out_of_scope": 0,
                "registered_ids": [source["id"]],
                "pending_entries": [],
            }

    class Pipeline:
        def __init__(self, *, companies_path, **_kwargs):
            self.companies_path = companies_path
            self.progress_callback = None

        def run(self, *, dry_run=False):
            assert dry_run is False
            companies = load_companies(self.companies_path)
            observed.append([company.name for company in companies])
            return SimpleNamespace(
                written=False,
                to_dict=lambda: {
                    "total_companies": len(companies),
                    "selected_companies": len(companies),
                    "crawled_companies": len(companies),
                    "companies": [],
                    "written": False,
                },
            )


    monkeypatch.setattr(scheduler_runtime, "OfferBiuRefreshService", RefreshService)
    monkeypatch.setattr(scheduler_runtime, "DailyRecruitmentPipeline", Pipeline)
    settings = Settings(
        agent_root=tmp_path,
        database_url=database_url,
        discovery_enabled=True,
        offline_reconciliation_enabled=False,
        llm_enabled=False,
    )
    handlers = scheduler_runtime.build_runtime_task_handlers(settings=settings)
    context = TaskContext(
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="test",
        scheduled_for=datetime(2026, 9, 12, tzinfo=timezone.utc),
        run_id="full-run",
        attempt=1,
        write_enabled=True,
        metadata={"details": {"mode": "full"}},
    )

    result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](context)

    assert result["status"] == "completed"
    assert result["offerbiu_companies_queued"] == 1
    assert observed[-1] == ["Current Company"]


@pytest.mark.parametrize(
    ("industry_groups", "refresh_error"),
    [
        (["internet-tech", "manufacturing-equipment", "auto-transport-equipment"], None),
        (["finance"], "当前 OfferBiu 发现适配器不支持所选行业范围"),
    ],
)
def test_daily_run_never_falls_back_to_legacy_after_biu_scope_attempt(
    tmp_path: Path,
    monkeypatch,
    industry_groups: list[str],
    refresh_error: str | None,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "companies.yaml").write_text(
        "companies:\n  - id: legacy-only\n    name: Legacy Only\n"
        "    careers_url: https://legacy.example.com/campus\n"
        "    crawler: render\n    integration_status: connected\n",
        encoding="utf-8",
    )
    (config_dir / "candidate_profile.yaml").write_text("profile: {}\n", encoding="utf-8")
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'agent.db').as_posix()}"
    storage = Storage.from_url(database_url, initialize=True)

    class RefreshService:
        def __init__(self, _registry):
            if refresh_error:
                raise AssertionError("unsupported scope must fail before refresh")
            self.last_registered_ids = ()

        def refresh(self, **_kwargs):
            return {
                "complete": True,
                "stop_reason": "complete",
                "pages_fetched": 1,
                "records_seen": 0,
                "companies_seen": 0,
                "applied": True,
                "registered_entries": 0,
                "new_entries": 0,
                "linked_existing_entries": 0,
                "excluded_unusable": 0,
                "out_of_scope": 0,
                "registered_ids": [],
                "pending_entries": [],
            }

    class Pipeline:
        def __init__(self, **_kwargs):
            self.progress_callback = None

        def run(self, **_kwargs):
            raise AssertionError("legacy companies must not be crawled")

    monkeypatch.setattr(scheduler_runtime, "OfferBiuRefreshService", RefreshService)
    monkeypatch.setattr(scheduler_runtime, "DailyRecruitmentPipeline", Pipeline)
    settings = Settings(
        agent_root=tmp_path,
        database_url=database_url,
        discovery_enabled=True,
        offerbiu_industry_groups=industry_groups,
        offline_reconciliation_enabled=False,
        llm_enabled=False,
    )
    handlers = scheduler_runtime.build_runtime_task_handlers(settings=settings)
    context = TaskContext(
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="test",
        scheduled_for=datetime(2026, 9, 12, tzinfo=timezone.utc),
        run_id="strict-scope-run",
        attempt=1,
        write_enabled=True,  # Only the temporary fixture database is authorized.
        metadata={"details": {"mode": "full"}},
    )

    result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](context)

    assert result["status"] == "failed"
    crawl_errors = [
        event["error"]
        for event in result["daily_sync"]["stages"]
        if event["stage"] == "crawl" and event["error"]
    ]
    assert crawl_errors
    if refresh_error:
        assert refresh_error in result["daily_sync"]["warnings"][0]
    else:
        assert "refusing legacy companies fallback" in crawl_errors[0]
