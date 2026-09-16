from hashlib import sha256
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api import company_sources, main
from packages.discovery import company_source_retry
from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery.company_source_retry import CompanySourceRetryService
from packages.storage import Storage


BASE_URL = "http://127.0.0.1:8137"
HEADERS = {"Origin": BASE_URL, "X-RecruitOps-Local-UI": "1"}


def test_main_company_sources_route_middleware_and_lazy_retry_wiring(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'company-sources-main.db'}",
        write_enabled=True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(company_sources, "get_settings", lambda: settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    registry = CompanySourceRegistry(storage)
    calls = []
    jd = "负责服务开发。"

    def fake_process(**kwargs):
        calls.append(kwargs)
        return {
            "company": kwargs["company"],
            "source_url": kwargs["source_url"],
            "jobs": [{
                "id": "job-1",
                "title": "Software Engineer",
                "detail_url": f"{kwargs['source_url']}/job-1",
                "jd_raw": jd,
                "capture_evidence": {
                    "status": "complete",
                    "identity_verified": True,
                    "terminal_observed": True,
                    "source_url": f"{kwargs['source_url']}/job-1",
                    "method": "rendered_detail",
                    "content_sha256": sha256(jd.encode()).hexdigest(),
                },
                "batch": "formal",
            }],
            "pages_seen": 1,
            "total_pages": 1,
            "has_more": False,
            "pagination_complete": True,
            "completeness_known": True,
            "advertised_total": 1,
        }

    service = CompanySourceRetryService(registry, process=fake_process, max_concurrency=2, timeout_seconds=75)
    original_retry_factory = company_source_retry.CompanySourceRetryService
    monkeypatch.setattr(
        company_source_retry,
        "CompanySourceRetryService",
        lambda _registry, *, max_concurrency, timeout_seconds: service,
    )
    main.get_company_source_retry_service.cache_clear()
    try:
        assert company_sources.start_retry is main._start_company_source_retry
        client = TestClient(main.app, base_url=BASE_URL)

        retry_row = registry.upsert_source(
            source="offerbiu",
            source_record_id="retry-1",
            company_name="Retry Co",
            source_url="https://example.jobs.feishu.cn/source",
            entry_url="https://example.jobs.feishu.cn/campus/position",
        )
        running_row = registry.upsert_source(
            source="offerbiu",
            source_record_id="running-1",
            company_name="Running Co",
            source_url="https://example.jobs.feishu.cn/source",
            entry_url="https://example.jobs.feishu.cn/campus/position",
        )
        registry.record_attempt(
            running_row["id"],
            status="running",
            attempted_url=running_row["entry_url"],
        )
        unsafe_row = registry.upsert_source(
            source="offerbiu",
            source_record_id="unsafe-1",
            company_name="Unsafe Co",
            source_url="file:///snapshot/source.json",
            entry_url="https://user:secret@example.test/jobs?token=secret",
        )

        listed = client.get("/api/company-sources?page=1&page_size=30", headers=HEADERS)
        assert listed.status_code == 200
        assert listed.json()["total"] == 3

        stale_patch = client.patch(
            f"/api/company-sources/{running_row['id']}/entry",
            headers=HEADERS,
            json={
                "entry_url": "https://example.jobs.feishu.cn/campus/manual",
                "expected_updated_at": registry.get(running_row["id"])["updated_at"],
            },
        )
        assert stale_patch.status_code == 409
        assert client.patch(
            f"/api/company-sources/{running_row['id']}/entry",
            headers={**HEADERS, "Origin": "https://evil.example"},
            json={
                "entry_url": "https://example.jobs.feishu.cn/campus/manual",
                "expected_updated_at": registry.get(running_row["id"])["updated_at"],
            },
        ).status_code == 403

        unsafe_detail = client.get(f"/api/company-sources/{unsafe_row['id']}", headers=HEADERS)
        assert unsafe_detail.status_code == 200
        unsafe_payload = unsafe_detail.json()
        assert unsafe_payload["source_url"] == "file:///snapshot/source.json"
        assert unsafe_payload["entry_url"] == "https://[REDACTED]@example.test/jobs?token=%5BREDACTED%5D"
        assert "secret" not in unsafe_detail.text

        response = client.post(f"/api/company-sources/{retry_row['id']}/retry", headers=HEADERS)
        assert response.status_code == 202
        assert response.json() == {"id": retry_row["id"], "status": "running"}
        deadline = time.time() + 3
        while service.active_count and time.time() < deadline:
            time.sleep(0.01)
        assert service.active_count == 0
        detail = client.get(f"/api/company-sources/{retry_row['id']}", headers=HEADERS).json()
        assert detail["status"] == "complete"
        assert detail["job_count"] == 1
        assert detail["jd_pending_count"] == 0
        assert detail["attempts_total"] == 2
        assert [attempt["status"] for attempt in detail["attempts"][:2]] == ["complete", "running"]
        assert len(calls) == 1
    finally:
        main.get_company_source_retry_service.cache_clear()
        company_source_retry.CompanySourceRetryService = original_retry_factory
