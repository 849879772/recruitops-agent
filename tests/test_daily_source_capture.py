from datetime import datetime, timezone
from hashlib import sha256

import yaml
from sqlalchemy import select

from packages.discovery.company_registry import CompanySourceRegistry
from packages.pipeline.daily import DailyRecruitmentPipeline, CrawlResult
from packages.storage import Storage, JobSnapshot


def _config(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump({"companies": [
        {"id": "one", "name": "One", "careers_url": "https://jobs.example.com/campus",
         "crawler": "fake", "integration_status": "connected"},
        {"id": "two", "name": "Two", "careers_url": "https://jobs.example.com/other",
         "integration_status": "pending"},
    ]}), encoding="utf-8")
    return path


def test_failed_company_remains_visible_without_job_rows(tmp_path):
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)

    def failed(_company):
        raise RuntimeError("fixture timeout")

    DailyRecruitmentPipeline(companies_path=_config(tmp_path), storage=storage,
                             crawler=failed, jd_hydrator=None).run()
    records = CompanySourceRegistry(storage).list_sources()["items"]
    assert len(records) == 2
    by_name = {record["company_name"]: record for record in records}
    assert by_name["One"]["status"] == "failed"
    assert by_name["One"]["entry_url"] == "https://jobs.example.com/campus"
    assert by_name["Two"]["reason_code"] == "not_connected"
    with storage.session() as session:
        assert session.scalars(select(JobSnapshot)).all() == []
    storage.engine.dispose()


def test_short_official_detail_flows_to_matcher_and_storage(tmp_path):
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)
    detail = "Develop C++ software on Linux（官方原文）．"
    url = "https://jobs.example.com/jobs/1"
    evidence = {"status": "complete", "method": "official_api", "source_url": url,
                "identity_verified": True, "terminal_observed": True, "remaining_controls": [],
                "content_sha256": sha256(detail.encode()).hexdigest(),
                "captured_at": datetime.now(timezone.utc).isoformat()}
    calls = []

    def matcher(job):
        calls.append(job)
        return {"analysis_status": "complete", "match_score": 77, "summary": "fixture"}

    result = DailyRecruitmentPipeline(
        companies_path=_config(tmp_path), storage=storage, matcher=matcher,
        crawler=lambda _company: CrawlResult(
            jobs=[{"id": "job-1", "title": "C++ Software Engineer", "detail_url": url,
                   "jd_raw": "", "cohort": 2027, "cohort_status": "confirmed", "batch": "formal"}],
            source_url="https://jobs.example.com/campus", pagination_complete=True,
            completeness_known=True, pages_seen=1, total_pages=1, advertised_total=1,
        ),
        jd_hydrator=lambda job: {"detail": detail, "status": "complete", "detail_url": url,
                                "capture_evidence": evidence},
    ).run()
    assert result.written
    assert len(calls) == 1
    assert calls[0]["jd_raw"] == detail
    with storage.session() as session:
        job = session.scalars(select(JobSnapshot)).one()
        assert job.capture_evidence == evidence
        assert job.match_score == 77
    record = next(x for x in CompanySourceRegistry(storage).list_sources()["items"] if x["company_name"] == "One")
    assert record["status"] == "complete"
    assert record["jd_pending_count"] == 0
    storage.engine.dispose()


def test_dry_run_does_not_persist_source_records(tmp_path):
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)
    DailyRecruitmentPipeline(companies_path=_config(tmp_path), storage=storage,
                             crawler=lambda company: CrawlResult(), jd_hydrator=None).run(dry_run=True)
    assert CompanySourceRegistry(storage).list_sources()["total"] == 0
    storage.engine.dispose()
