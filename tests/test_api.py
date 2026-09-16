from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api import main as api
from apps.api.main import app
from packages.approval import (
    ApprovalPreview,
    ApprovalRegistry,
    OperationName,
    WriteAuditRecord,
)


def test_health_reports_read_only_mode(monkeypatch) -> None:
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=False))
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "read_only"}


def test_retired_oc_routes_are_not_registered() -> None:
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]

    assert not any(path.startswith("/api/oc/") for path in paths)
    assert "/api/browser/oc-snapshots" not in paths


def test_health_reports_approval_gated_mode(monkeypatch) -> None:
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=True))
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "approval_gated"}


def test_readiness_checks_agent_dependencies(monkeypatch, tmp_path) -> None:
    companies_config = tmp_path / "companies.yaml"
    candidate_profile = tmp_path / "candidate_profile.yaml"
    for path in (companies_config, candidate_profile):
        path.write_text("", encoding="utf-8")
    settings = SimpleNamespace(
        companies_config=companies_config,
        candidate_profile_config=candidate_profile,
        write_enabled=False,
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            assert str(statement) == "SELECT 1"

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_independent_crawler_available", lambda: True)
    monkeypatch.setattr(api, "get_storage_engine", lambda: Engine())

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert all(response.json()["checks"].values())


def test_readiness_returns_503_when_agent_state_is_missing(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(
        companies_config=tmp_path / "missing-companies.yaml",
        candidate_profile_config=tmp_path / "missing-candidate-profile.yaml",
        write_enabled=False,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "_independent_crawler_available", lambda: True)
    monkeypatch.setattr(api, "get_storage_engine", lambda: (_ for _ in ()).throw(OSError()))

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["companies_config"] is False
    assert response.json()["checks"]["candidate_profile"] is False
    assert response.json()["checks"]["postgres"] is False
    assert not any(key.startswith("source_") for key in response.json()["checks"])


def test_api_defaults_to_agent_postgres(monkeypatch) -> None:
    calls: list[object] = []

    class StorageFactory:
        @classmethod
        def from_url(cls, database_url):
            calls.append(("storage", database_url))
            return "agent-storage"

    class RepositoryFactory:
        def __init__(self, storage):
            calls.append(("repository", storage))

    settings = SimpleNamespace(
        database_url="postgresql+psycopg://agent",
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "Storage", StorageFactory)
    monkeypatch.setattr(api, "PostgresRecruitmentRepository", RepositoryFactory)
    api.get_repository.cache_clear()
    try:
        assert isinstance(api.get_repository(), RepositoryFactory)
    finally:
        api.get_repository.cache_clear()

    assert calls == [
        ("storage", "postgresql+psycopg://agent"),
        ("repository", "agent-storage"),
    ]


def test_dashboard_is_served_without_shadowing_api_routes() -> None:
    client = TestClient(app)
    page = client.get("/")
    asset = client.get("/styles.css")

    assert page.status_code == 200
    assert "RecruitOps" in page.text
    assert asset.status_code == 200
    assert "--page-bg" in asset.text


def test_local_automation_routes_list_latest_execution_and_require_token_to_disable(
    monkeypatch,
) -> None:
    schedule = SimpleNamespace(
        id="automation-1",
        task_id="application_progress",
        task_label="投递复核",
        target_kind="application",
        target_id="24",
        target_label="新华三 软件开发工程师",
        frequency="daily",
        start_time=datetime.strptime("03:00", "%H:%M").time(),
        timezone_name="Asia/Shanghai",
        active=True,
        next_run_at=datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc),
        last_run_at=datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc),
        last_status="succeeded",
        last_error=None,
    )
    execution = SimpleNamespace(
        id="automation-run-1",
        status="succeeded",
        scheduled_for=schedule.last_run_at,
        started_at=schedule.last_run_at,
        completed_at=schedule.last_run_at + timedelta(minutes=1),
        result_summary="状态未变化",
        error=None,
        thread_id="thread-1",
    )

    class StorageFactory:
        @classmethod
        def from_url(cls, database_url):
            assert database_url == "postgresql+psycopg://agent"
            return "automation-storage"

    class Store:
        def __init__(self, storage):
            assert storage == "automation-storage"

        def list(self, *, active_only=False):
            return [schedule] if not active_only or schedule.active else []

        def executions(self, schedule_id, *, limit=20):
            assert schedule_id == schedule.id
            assert limit == 1
            return [execution]

        def disable(self, schedule_id):
            assert schedule_id == schedule.id
            schedule.active = False
            return schedule

    monkeypatch.setattr(api, "Storage", StorageFactory)
    monkeypatch.setattr(api, "AutomationStore", Store)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+psycopg://agent",
            write_enabled=True,
            api_token="secret",
        ),
    )
    client = TestClient(app)

    listed = client.get("/api/automations")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["latest_execution"]["result_summary"] == "状态未变化"
    assert client.post("/api/automations/automation-1/disable").status_code == 401

    disabled = client.post(
        "/api/automations/automation-1/disable",
        headers={"Authorization": "Bearer secret"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False


def test_operational_report_is_read_only_and_returns_repair_candidates() -> None:
    from tests.test_typed_tools import InMemoryRepository

    app.dependency_overrides[api.repository] = lambda: InMemoryRepository()
    try:
        response = TestClient(app).get("/api/reports/operational?on=2026-08-19")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["date"] == "2026-08-19"
    assert payload["safety"] == {
        "write_attempted": False,
        "model_call_attempted": False,
        "external_web_access_attempted": False,
    }
    assert isinstance(payload["repair_candidates"], list)


def test_application_page_is_bounded_and_keeps_legacy_endpoint() -> None:
    from tests.test_typed_tools import InMemoryRepository

    repository = InMemoryRepository()
    repository.applications = repository.applications[:1] * 3
    app.dependency_overrides[api.repository] = lambda: repository
    try:
        page = TestClient(app).get("/api/applications/page?limit=2&offset=1")
        legacy = TestClient(app).get("/api/applications")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert legacy.status_code == 200
    assert isinstance(legacy.json(), list)


def test_job_browse_endpoint_is_2027_only_and_bounded() -> None:
    from tests.test_postgres_repository import _repository

    app.dependency_overrides[api.repository] = _repository
    try:
        response = TestClient(app).get(
            "/api/jobs/browse?category=robotics&score_band=high&limit=20"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 20
    assert payload["items"][0]["category"] == "robotics"
    assert "jd_raw" not in payload["items"][0]
    assert payload["stats"]["jobs"] == 1
    assert payload["facets"]["companies"][0]["name"] == "示例科技"


def test_job_browse_accepts_explicit_screening_states() -> None:
    from tests.test_postgres_repository import _repository

    app.dependency_overrides[api.repository] = _repository
    try:
        client = TestClient(app)
        for state in ("pending", "jd_incomplete", "excluded"):
            response = client.get(f"/api/jobs/browse?evaluation={state}")
            assert response.status_code == 200
            assert response.json()["total"] == 0
            assert response.json()["stats"]["pending"] == 0
        assert client.get("/api/jobs/browse?evaluation=made_up").status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_browser_observation_endpoint_accepts_only_typed_sanitized_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="browser-secret"),
    )
    client = TestClient(app)
    request = {
        "url": "https://example.com/campus",
        "allowed_origins": ["https://example.com"],
        "title": "2027 校招",
        "page_text": "岗位职责：C++",
    }

    assert client.post("/api/browser/observations", json=request).status_code == 401
    response = client.post(
        "/api/browser/observations",
        json=request,
        headers={"Authorization": "Bearer browser-secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool_name"] == "browser_observation"
    assert payload["read_only"] is True
    assert payload["data"]["status"] == "observed"


def test_browser_application_capture_enqueues_but_does_not_execute_write(monkeypatch) -> None:
    from tests.test_typed_tools import InMemoryRepository

    repository = InMemoryRepository()
    repository.applications = []
    registry = ApprovalRegistry()
    monkeypatch.setattr(api, "approval_registry", registry)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="capture-secret", write_enabled=False),
    )
    app.dependency_overrides[api.repository] = lambda: repository
    try:
        response = TestClient(app).post(
            "/api/browser/application-captures",
            json={
                "request_id": "capture-api-1",
                "url": "https://example.com/jobs/1",
                "title": "C++开发工程师",
                "page_text": "示例公司 C++开发工程师 投递成功",
            },
            headers={"Authorization": "Bearer capture-secret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approval_required"
    assert payload["approval"]["status"] == "pending"
    assert payload["approval"]["token"]["operation"] == "application_create"
    assert repository.applications == []


def test_write_execution_requires_configured_local_token(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="", write_enabled=True),
    )

    response = TestClient(app).post(
        "/api/approvals/token-1/execute",
        json={"operator": "tester"},
    )

    assert response.status_code == 503


def test_approval_mutations_require_configured_local_token(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="approval-secret", write_enabled=False),
    )
    client = TestClient(app)
    preview = ApprovalPreview(
        task_id="task-approval-create-auth",
        operation=OperationName.SCHEDULE_CREATE,
        target_id="schedule-create-1",
        before={},
        after={"title": "笔试"},
        evidence_summary="用户明确要求创建日程。",
        idempotency_key="approval-create-auth:schedule-1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    assert client.post(
        "/api/approvals",
        json=preview.model_dump(mode="json"),
    ).status_code == 401
    assert client.post(
        "/api/approvals",
        json=preview.model_dump(mode="json"),
        headers={"Authorization": "Bearer wrong-secret"},
    ).status_code == 401

    for path in (
        "/api/approvals/token-1/approve",
        "/api/approvals/token-1/reject",
    ):
        assert client.post(path).status_code == 401
        assert client.post(
            path,
            headers={"Authorization": "Bearer wrong-secret"},
        ).status_code == 401


def test_approval_decision_is_allowed_after_bearer_auth(monkeypatch) -> None:
    registry = ApprovalRegistry()
    preview = ApprovalPreview(
        task_id="task-approval-auth",
        operation=OperationName.SCHEDULE_CREATE,
        target_id="schedule-1",
        before={},
        after={"title": "笔试"},
        evidence_summary="用户明确要求创建日程。",
        idempotency_key="approval-auth:schedule-1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    issued = registry.issue(preview)
    assert issued.token is not None
    monkeypatch.setattr(api, "approval_registry", registry)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="approval-secret", write_enabled=False),
    )

    response = TestClient(app).post(
        f"/api/approvals/{issued.token.token_id}/approve",
        headers={"Authorization": "Bearer approval-secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_write_execution_uses_approved_executor_after_bearer_auth(monkeypatch) -> None:
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)

    class Executor:
        def execute(self, token_id, *, operator):
            assert token_id == "token-1"
            assert operator == "local-user"
            return WriteAuditRecord(
                execution_id="execution-1",
                token_id=token_id,
                task_id="task-1",
                operation=OperationName.SCHEDULE_CREATE,
                idempotency_key="schedule-1",
                operator=operator,
                evidence_digest="digest",
                started_at=now,
                completed_at=now,
                success=True,
                after={"event_id": 1},
            )

    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="secret", write_enabled=True),
    )
    monkeypatch.setattr(api, "get_write_executor", lambda: Executor())

    response = TestClient(app).post(
        "/api/approvals/token-1/execute",
        json={"operator": "local-user"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_recruitment_mail_api_is_read_only_and_returns_review_previews(monkeypatch) -> None:
    from datetime import datetime, timezone

    from packages.recruitment_mail import (
        CompanyCandidate,
        JobCandidate,
        MailIdentity,
        ParsedRecruitmentEmail,
        RecruitmentMailStore,
        RecruitmentMessageCategory,
    )
    from packages.recruitment_mail.analysis_store import save_model_analysis
    from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
    from packages.storage import Storage
    from tests.test_typed_tools import InMemoryRepository

    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="", database_url="sqlite:///:memory:"),
    )
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    record = store.upsert(
        ParsedRecruitmentEmail(
            identity=MailIdentity(message_id="api-mail-1"),
            sender="招聘团队",
            subject="示例公司 C++开发工程师面试邀请",
            body_text="请参加面试",
            received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            category=RecruitmentMessageCategory.INTERVIEW,
            company_candidates=[
                CompanyCandidate(value="示例公司", evidence="主题", confidence=1.0)
            ],
            job_candidates=[
                JobCandidate(value="C++开发工程师", evidence="主题", confidence=1.0)
            ],
            confidence=0.95,
        )
    )
    repository = InMemoryRepository()
    save_model_analysis(
        store,
        record.id,
        record.content_digest,
        MAIL_ANALYSIS_VERSION,
        {
            "record_id": record.id,
            "content_digest": record.content_digest,
            "company_name": "示例公司",
            "job_title": "C++开发工程师",
            "job_code": None,
            "event_type": "interview",
            "event_time": None,
            "deadline": None,
            "evidence_quotes": [record.subject],
            "candidate_application_id": str(repository.applications[0].id),
            "match_reason": "explicit source-bound fixture",
            "action_summary": None,
        },
        "proposed",
        model="fixture-model",
    )
    original_stage = repository.applications[0].stage
    app.dependency_overrides[api.recruitment_mail_store] = lambda: store
    app.dependency_overrides[api.repository] = lambda: repository
    try:
        listed = TestClient(app).get("/api/recruitment-mails?category=interview")
        detailed = TestClient(app).get(f"/api/recruitment-mails/{record.id}")
        reviewed = TestClient(app).post(
            f"/api/recruitment-mails/{record.id}/review",
            json={},
        )
    finally:
        app.dependency_overrides.clear()

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == record.id
    assert detailed.status_code == 200
    assert detailed.json()["record_id"] == record.id
    assert reviewed.status_code == 200
    assert reviewed.json()["approval_previews"]
    assert repository.applications[0].stage is original_stage


def test_recruitment_mail_review_obeys_local_bearer_auth(monkeypatch) -> None:
    from packages.recruitment_mail import RecruitmentMailStore
    from packages.storage import Storage
    from tests.test_typed_tools import InMemoryRepository

    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(api_token="mail-secret", database_url="sqlite:///:memory:"),
    )
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    app.dependency_overrides[api.recruitment_mail_store] = lambda: store
    app.dependency_overrides[api.repository] = lambda: InMemoryRepository()
    try:
        client = TestClient(app)
        path = "/api/recruitment-mails/missing/review"
        assert client.post(path, json={}).status_code == 401
        assert (
            client.post(
                path,
                json={},
                headers={"Authorization": "Bearer mail-secret"},
            ).status_code
            == 404
        )
    finally:
        app.dependency_overrides.clear()
