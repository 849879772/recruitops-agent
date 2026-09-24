"""Synthetic-only repository benchmark: python tests/benchmark_job_browse.py."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage
from packages.storage.models import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot


def populate(storage, count=10_000):
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with storage.engine.begin() as connection:
        connection.execute(CompanySnapshot.__table__.insert(), [
            dict(id=f"company-{i}", name=f"Company {i:03}", integration_status="connected",
                 organization_id=f"org-{i}", source="benchmark", source_ref=f"company-{i}")
            for i in range(50)
        ])
        connection.execute(JobSnapshot.__table__.insert(), [
            dict(id=f"job-{i:05}", company_id=f"company-{i % 50}",
                 title=f"C++ 软件工程师 {i:05}" if i % 20 else f"销售专员 {i:05}",
                 city="上海", detail_url=f"https://example.test/{i}", cohort=2027,
                 cohort_status="confirmed", batch="formal", capture_status="complete",
                 match_score=i % 101, first_seen_at=now + timedelta(seconds=i),
                 source="benchmark", source_ref=f"job-{i}")
            for i in range(count)
        ])
        connection.execute(JobAnalysisSnapshot.__table__.insert(), [
            dict(job_id=f"job-{i:05}", analysis_status="complete", match_score=i % 101,
                 summary="Synthetic summary " * 80, advantages='["Synthetic project"]',
                 gaps='["Synthetic missing skill"]', source="benchmark", source_ref=f"analysis-{i}")
            for i in range(count)
        ])


if __name__ == "__main__":
    with TemporaryDirectory(prefix="recruitops-browse-benchmark-") as directory:
        storage = Storage.from_url(f"sqlite:///{Path(directory) / 'synthetic.db'}", initialize=True)
        populate(storage)
        repo = PostgresRecruitmentRepository(storage)
        with patch("packages.config.get_settings", return_value=SimpleNamespace(candidate_profile_config=Path(directory) / "missing.yaml")):
            times = []
            for page in range(3):
                start = perf_counter()
                result = repo.browse_jobs(limit=50, offset=page * 50)
                times.append(round((perf_counter() - start) * 1000, 2))
            print({"rows": 10_000, "eligible": result.total, "default_page_ms": times})
            if "include_summary" in __import__("inspect").signature(repo.browse_jobs).parameters:
                start = perf_counter()
                result = repo.browse_jobs(limit=50, offset=150, include_summary=False)
                print({"light_page_ms": round((perf_counter() - start) * 1000, 2), "items": len(result.items)})
        storage.engine.dispose()
