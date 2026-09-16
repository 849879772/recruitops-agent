from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import yaml

from packages.pipeline import CrawlResult, DailyRecruitmentPipeline
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.scheduler import TaskContext, TaskType, build_runtime_task_handlers
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage


UTC = timezone.utc
LIST_URL = "https://scheduled.example.test/campus"


def test_scheduled_handler_reuses_title_first_daily_pipeline(tmp_path: Path) -> None:
    companies_path = tmp_path / "companies.yaml"
    companies_path.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "scheduled-company",
                        "name": "Scheduled Company",
                        "careers_url": f"{LIST_URL}/entry",
                        "crawler": "fixture",
                        "integration_status": "connected",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    storage = Storage.from_url(
        f"sqlite:///{(tmp_path / 'scheduled.sqlite').as_posix()}",
        initialize=True,
    )
    detail_text = "Short official detail"

    class Crawler:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _company: Any) -> CrawlResult:
            self.calls += 1
            return CrawlResult(
                jobs=[
                    {
                        "id": "scheduled-1",
                        "title": "C++ Software Engineer",
                        "detail_url": f"{LIST_URL}/jobs/1",
                        "cohort": 2027,
                        "cohort_status": "confirmed",
                        "batch": "formal",
                    }
                ],
                source_url=LIST_URL,
                allowed_origins=("https://scheduled.example.test",),
                pages_seen=1,
                total_pages=1,
                pagination_complete=True,
                completeness_known=True,
            )

    class Hydrator:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
            self.calls += 1
            url = str(job["detail_url"])
            return {
                "status": "complete",
                "detail": detail_text,
                "detail_url": url,
                "capture_evidence": {
                    "status": "complete",
                    "method": "fixture",
                    "source_url": url,
                    "identity_verified": True,
                    "terminal_observed": True,
                    "remaining_controls": [],
                    "content_sha256": sha256(detail_text.encode("utf-8")).hexdigest(),
                },
            }

    class Matcher:
        def __init__(self) -> None:
            self.calls = 0

        def analyze_title_first(
            self,
            _job: Mapping[str, Any],
            _profile: Any,
            *,
            existing_analysis: Any = None,
            screening: Any = None,
        ) -> dict[str, Any]:
            assert existing_analysis is None
            assert screening is not None
            assert screening.eligible is True
            self.calls += 1
            return {"analysis_status": "complete", "match_score": 73}

    crawler = Crawler()
    hydrator = Hydrator()
    matcher = Matcher()
    pipeline = DailyRecruitmentPipeline(
        companies_path=companies_path,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
    )
    repository = PostgresRecruitmentRepository(storage)
    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(
            mail_enabled=False,
            discovery_enabled=False,
            offline_reconciliation_enabled=False,
            llm_enabled=False,
            job_analysis_enabled=False,
        ),
        repository=repository,
        daily_pipeline=pipeline,
    )

    result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
        TaskContext(
            task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
            task_label="scheduled title-first",
            scheduled_for=datetime(2026, 9, 9, tzinfo=UTC),
            run_id="scheduled-title-first-run",
            attempt=1,
            write_enabled=True,
            metadata={"details": {"requested_dry_run": False}},
        )
    )

    assert result["status"] == "completed"
    assert result["daily_sync"]["status"] == "succeeded"
    assert result["daily_sync"]["pipeline"]["new"] == 1
    assert crawler.calls == 1
    assert hydrator.calls == 1
    assert matcher.calls == 1
    with storage.session() as session:
        job = session.get(JobSnapshot, "scheduled-1")
        assert job is not None
        assert job.capture_status == "complete"
        assert session.get(JobAnalysisSnapshot, "scheduled-1") is not None
