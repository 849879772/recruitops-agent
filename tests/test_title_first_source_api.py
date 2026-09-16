from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import company_sources
from apps.api import main as api
from packages.discovery.company_registry import CompanySourceRegistry
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import JobSnapshot, Storage


def _app(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'source-api.db'}",
        write_enabled=False,
    )
    monkeypatch.setattr(company_sources, "get_settings", lambda: settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    app = FastAPI()
    app.include_router(company_sources.router)
    return TestClient(app), storage, CompanySourceRegistry(storage)


def _job(job_id: str, company_id: str, title: str, *, availability_status: str = "active"):
    return JobSnapshot(
        id=job_id,
        company_id=company_id,
        title="软件工程师 " + title,
        detail_url=f"https://jobs.example/{job_id}",
        cohort=2027,
        cohort_status="confirmed",
        batch="formal",
        capture_status="failed" if job_id == "failed" else "complete",
        capture_failure_reason="detail_timeout" if job_id == "failed" else "",
        availability_status=availability_status,
        match_score=None if job_id == "failed" else 82,
        source="fixture",
        source_ref=f"job:{job_id}",
    )


def test_source_jobs_endpoint_is_company_scoped_and_keeps_failed_inactive_rows(
    monkeypatch, tmp_path
):
    client, storage, registry = _app(monkeypatch, tmp_path)
    source = registry.upsert_source(
        source="offerbiu",
        source_record_id="source-1",
        company_name="Company One",
        company_id="company-1",
        source_url="https://offerbiu.example/sources",
        entry_url="https://jobs.example/company-1",
    )
    with storage.write_transaction() as session:
        session.add_all(
            [
                _job("failed", "company-1", "Failed detail", availability_status="inactive"),
                _job("active", "company-1", "Active detail"),
                _job("other", "company-2", "Other company"),
            ]
        )

    response = client.get(f"/api/company-sources/{source['id']}/jobs?page=1&page_size=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["company_id"] == "company-1"
    assert set(payload["items"][0]) >= {
        "id",
        "company_id",
        "title",
        "detail_url",
        "capture_status",
        "capture_failure_reason",
        "availability_status",
        "match_score",
    }

    second_page = client.get(f"/api/company-sources/{source['id']}/jobs?page=2&page_size=1")
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["id"] == "failed"
    assert second_page.json()["items"][0]["capture_failure_reason"] == "detail_timeout"
    assert second_page.json()["items"][0]["availability_status"] == "inactive"


def test_source_jobs_endpoint_returns_empty_for_unbound_or_empty_sources(
    monkeypatch, tmp_path
):
    client, storage, registry = _app(monkeypatch, tmp_path)
    unbound = registry.upsert_source(
        source="offerbiu",
        source_record_id="unbound",
        company_name="Unbound company",
        source_url="https://offerbiu.example/sources",
        entry_url="https://jobs.example/unbound",
    )
    empty = registry.upsert_source(
        source="offerbiu",
        source_record_id="empty",
        company_name="Empty company",
        company_id="company-empty",
        source_url="https://offerbiu.example/sources",
        entry_url="https://jobs.example/empty",
    )
    with storage.write_transaction() as session:
        session.add(_job("unrelated", "company-other", "Unrelated"))

    for source in (unbound, empty):
        response = client.get(f"/api/company-sources/{source['id']}/jobs")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 30}

    assert client.get("/api/company-sources/missing/jobs").status_code == 404


def test_ordinary_job_api_models_preserve_capture_and_availability_states():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    repository = PostgresRecruitmentRepository(storage)
    with storage.write_transaction() as session:
        session.add_all(
            [
                _job("complete", "company-1", "Complete detail"),
                _job("failed", "company-1", "Failed detail"),
                _job(
                    "inactive",
                    "company-1",
                    "Inactive detail",
                    availability_status="inactive",
                ),
            ]
        )

    api.app.dependency_overrides[api.repository] = lambda: repository
    try:
        client = TestClient(api.app)
        list_response = client.get("/api/jobs?cohort=2027&cohort_status=confirmed")
        assert list_response.status_code == 200
        listed = {item["id"]: item for item in list_response.json()["items"]}
        assert set(listed) == {"complete", "failed", "inactive"}
        assert listed["complete"]["capture_status"] == "complete"
        assert listed["failed"]["capture_status"] == "failed"
        assert listed["failed"]["capture_failure_reason"] == "detail_timeout"
        assert listed["inactive"]["availability_status"] == "inactive"

        detail_response = client.get("/api/jobs/failed")
        assert detail_response.status_code == 200
        assert detail_response.json()["job"]["capture_status"] == "failed"
        assert detail_response.json()["job"]["capture_failure_reason"] == "detail_timeout"

        browse_response = client.get("/api/jobs/browse")
        assert browse_response.status_code == 200
        browsed = {item["id"]: item for item in browse_response.json()["items"]}
        assert set(browsed) == {"complete", "failed", "inactive"}
        assert browsed["failed"]["capture_status"] == "failed"
        assert browsed["failed"]["capture_failure_reason"] == "detail_timeout"
        assert browsed["inactive"]["availability_status"] == "inactive"
    finally:
        api.app.dependency_overrides.clear()
