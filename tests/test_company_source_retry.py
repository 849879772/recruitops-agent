from threading import Event
import time

import pytest

from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery.company_source_retry import (
    CompanySourceRetryCapacity,
    CompanySourceRetryConflict,
    CompanySourceRetryRejected,
    CompanySourceRetryService,
)
from packages.storage import Storage


def _registry(tmp_path):
    return CompanySourceRegistry(
        Storage.from_url(f"sqlite:///{tmp_path / 'retry.db'}", initialize=True)
    )


def _row(registry, suffix):
    return registry.upsert_source(
        source="feed", source_record_id=suffix, company_name=f"Company {suffix}",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/jobs",
    )


def test_retry_is_bounded_duplicate_safe_and_writes_only_attempts(tmp_path):
    registry = _registry(tmp_path)
    rows = [_row(registry, str(index)) for index in range(3)]
    entered = Event()
    release = Event()

    def process(**kwargs):
        entered.set()
        release.wait(timeout=2)
        return {
            "jobs": [{
                "id": "1", "title": "Engineer", "detail_url": "https://jobs.example/jobs/1",
                "jd_raw": "负责服务开发。", "capture_evidence": {
                    "status": "complete", "identity_verified": True, "terminal_observed": True,
                    "source_url": "https://jobs.example/jobs/1", "method": "rendered_detail",
                    "content_sha256": "f6b986a6d1f1c8f4d0b7f0f0d2d5a0b6f5ed6f9b4f4b8a6f6b4e2e2f8c3a5a5f",
                }, "batch": "formal",
            }],
            "pages_seen": 1, "total_pages": 1, "has_more": False,
            "pagination_complete": True, "completeness_known": True,
            "advertised_total": 1,
        }

    service = CompanySourceRetryService(registry, process=process, timeout_seconds=1)
    service.start_retry(rows[0]["id"])
    service.start_retry(rows[1]["id"])
    assert entered.wait(timeout=1)
    with pytest.raises(CompanySourceRetryConflict):
        service.start_retry(rows[0]["id"])
    with pytest.raises(CompanySourceRetryCapacity):
        service.start_retry(rows[2]["id"])

    release.set()
    deadline = time.time() + 2
    while service.active_count and time.time() < deadline:
        time.sleep(0.01)
    assert service.active_count == 0
    assert registry.get(rows[0]["id"])["status"] == "partial"
    assert registry.get(rows[0]["id"])["attempts"][0]["job_count"] == 1


def test_retry_uses_strict_pagination_and_jd_evidence(tmp_path):
    registry = _registry(tmp_path)
    row = _row(registry, "evidence")
    complete_job = {
        "id": "1", "title": "Engineer", "detail_url": "https://jobs.example/jobs/1",
        "jd_raw": "完整 JD", "capture_evidence": {}, "batch": "formal",
    }

    service = CompanySourceRetryService(
        registry,
        process=lambda **kwargs: {
            "jobs": [complete_job], "pagination_complete": True,
            "completeness_known": True, "pages_seen": 1, "total_pages": 2,
            "has_more": "false", "advertised_total": 1,
        },
    )
    service.start_retry(row["id"])
    deadline = time.time() + 2
    while service.active_count and time.time() < deadline:
        time.sleep(0.01)
    attempt = registry.get(row["id"])["attempts"][0]
    assert attempt["status"] == "partial"
    assert attempt["pagination_complete"] is False
    assert attempt["reason_code"] == "pagination_incomplete"


def test_jd_pending_forces_partial_even_with_complete_pagination(tmp_path):
    registry = _registry(tmp_path)
    row = _row(registry, "jd-pending")
    service = CompanySourceRetryService(
        registry,
        process=lambda **kwargs: {
            "jobs": [{
                "id": "1", "title": "Engineer", "detail_url": "https://jobs.example/jobs/1",
                "jd_raw": "short", "capture_evidence": {}, "batch": "formal",
            }],
            "pages_seen": 1, "total_pages": 1, "has_more": "false",
            "pagination_complete": "true", "completeness_known": True,
            "advertised_total": 1,
        },
    )
    service.start_retry(row["id"])
    deadline = time.time() + 2
    while service.active_count and time.time() < deadline:
        time.sleep(0.01)
    attempt = registry.get(row["id"])["attempts"][0]
    assert attempt["status"] == "partial"
    assert attempt["failure_stage"] == "detail"
    assert attempt["jd_pending_count"] == 1


def test_retry_reuses_safe_url_guard_and_rejects_non_crawlable_entry(tmp_path):
    registry = _registry(tmp_path)
    row = _row(registry, "unsafe")
    with registry.storage.write_transaction() as session:
        from packages.discovery.company_registry import CompanySourceRecord

        session.get(CompanySourceRecord, row["id"]).entry_url = "https://user:pass@example.test/jobs"
    called = []
    service = CompanySourceRetryService(registry, process=lambda **kwargs: called.append(kwargs))
    with pytest.raises(CompanySourceRetryRejected):
        service.start_retry(row["id"])
    assert called == []
    assert registry.get(row["id"])["status"] == "unusable"


def test_retry_applies_shared_wechat_and_form_destination_filter(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="snapshot", source_record_id="wechat", company_name="WeChat",
        source_url="https://feed.example/rows", entry_url="https://mp.weixin.qq.com/s/notice",
    )
    called = []
    service = CompanySourceRetryService(registry, process=lambda **kwargs: called.append(kwargs))
    with pytest.raises(CompanySourceRetryRejected):
        service.start_retry(row["id"])
    assert called == []
    assert registry.get(row["id"])["attempts"][0]["reason_code"] == "article"
