"""Isolated completion security fixtures: no runtime, model, mailbox or database."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import configuration, local_ui
from packages.candidate_profile.models import CandidateProfile
from packages.config import Settings


@pytest.fixture
def completion_case(tmp_path, monkeypatch):
    root = tmp_path / "owned-instance"
    (root / "config").mkdir(parents=True)
    instance_id, run_id = "a" * 32, "b" * 32
    metadata = {"schema": 1, "instance_id": instance_id, "run_id": run_id,
                "root": str(root.resolve()), "postgres_major": 16, "state": "ready"}
    (root / "instance.json").write_text(json.dumps(metadata), encoding="utf-8")
    for key, value in {
        "RECRUITOPS_ENV": "desktop-isolated", "RECRUITOPS_AGENT_ROOT": str(root),
        "RECRUITOPS_DESKTOP_INSTANCE_ID": instance_id, "RECRUITOPS_DESKTOP_RUN_ID": run_id,
        "RECRUITOPS_DESKTOP_WRITE_OPTIN": instance_id, "RECRUITOPS_WRITE_ENABLED": "true",
        "RECRUITOPS_DESKTOP_CAPABILITIES": "{}",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None, agent_root=root, source_root=root / "unused-source",
        database_url="sqlite:///:memory:", write_enabled=True, llm_enabled=True,
        llm_api_key="synthetic-not-a-real-key", model_name="deepseek-flash",
        model_api_base_url="https://api.deepseek.com", job_analysis_enabled=False,
        codex_runtime_enabled=False, mail_enabled=False, mail_sync_on_startup=False,
        automation_enabled=False, vision_enabled=False, offerbiu_industry_groups=["finance"])
    profile = CandidateProfile(source_ref="fixture", content_hash="0" * 64,
        skills=["Synthetic testing"], matching={"title_keywords": ["Engineer"]})
    profile_data = profile.model_dump(exclude={"schema_version", "source_ref", "content_hash"})
    (root / "config/candidate_profile.yaml").write_text(json.dumps({"profile": profile_data}), encoding="utf-8")
    getter = lambda: settings
    getter.cache_clear = lambda: None
    monkeypatch.setattr(configuration, "get_settings", getter)
    app = FastAPI()

    @app.middleware("http")
    async def owner_boundary(request, call_next):
        token = local_ui.local_ui_request.set(local_ui.is_local_ui(request))
        try:
            return await call_next(request)
        finally:
            local_ui.local_ui_request.reset(token)

    app.include_router(configuration.router)
    client = TestClient(app, base_url="http://127.0.0.1:18119")
    headers = {"Origin": "http://127.0.0.1:18119", "X-RecruitOps-Local-UI": "1"}
    return SimpleNamespace(root=root, settings=settings, profile=profile, metadata=metadata,
        marker=root / "config/runtime-capabilities.json", client=client, headers=headers)


def complete(case, **changes):
    return case.client.post("/api/local-ui/configuration/save", headers=case.headers,
                            json={"complete_onboarding": True, **changes})


def test_explicit_completion_writes_exact_marker_only_after_validated_save(completion_case):
    case = completion_case
    response = complete(case)
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is True
    assert json.loads(case.marker.read_text(encoding="utf-8")) == {
        "schema": 1, "instance_id": "a" * 32, "first_run_complete": True}
    assert "synthetic-not-a-real-key" not in response.text + case.marker.read_text()
    assert not list(case.marker.parent.glob("*.tmp"))


def test_generic_save_never_marks_complete(completion_case):
    case = completion_case
    response = case.client.post("/api/local-ui/configuration/save", headers=case.headers, json={})
    assert response.status_code == 200, response.text
    assert not case.marker.exists()


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://external.example", "X-RecruitOps-Local-UI": "1"},
    {"Origin": "http://127.0.0.1:18119", "X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "cross-site"}])
def test_completion_rejects_unauthorized_origin(completion_case, headers):
    case = completion_case
    response = case.client.post("/api/local-ui/configuration/save", headers=headers,
                                json={"complete_onboarding": True})
    assert response.status_code == 403
    assert not case.marker.exists()


@pytest.mark.parametrize("name,value", [
    ("RECRUITOPS_DESKTOP_WRITE_OPTIN", ""), ("RECRUITOPS_DESKTOP_WRITE_OPTIN", "c" * 32),
    ("RECRUITOPS_WRITE_ENABLED", "false"), ("RECRUITOPS_DESKTOP_INSTANCE_ID", "c" * 32),
    ("RECRUITOPS_DESKTOP_RUN_ID", "c" * 32), ("RECRUITOPS_AGENT_ROOT", "relative-root"),
])
def test_completion_rejects_wrong_launch_or_root(completion_case, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    response = complete(completion_case)
    assert response.status_code == 403
    assert not completion_case.marker.exists()
    assert not (completion_case.root / ".data/settings/preferences.json").exists()


@pytest.mark.parametrize("field,value", [("instance_id", "c" * 32), ("run_id", "c" * 32),
    ("root", "wrong-root"), ("state", "stopped"), ("schema", 2), ("postgres_major", 15)])
def test_completion_rejects_instance_metadata_mismatch(completion_case, field, value):
    case = completion_case
    case.metadata[field] = value
    (case.root / "instance.json").write_text(json.dumps(case.metadata), encoding="utf-8")
    assert complete(case).status_code == 403
    assert not case.marker.exists()


@pytest.mark.parametrize("changes", [
    {"settings": {"llm_api_key": "   "}},
    {"profile": {"skills": [], "matching": {"title_keywords": ["Engineer"]}}},
    {"profile": {"skills": ["Testing"], "matching": {"title_keywords": []}}},
    {"profile": {"skills": ["Testing"], "matching": {"title_keywords": ["Engineer"]}, "scope": {"industry_groups": []}}},
    {"settings": {"llm_enabled": False, "job_analysis_enabled": True}},
])
def test_incomplete_configuration_does_not_write_marker_or_preferences(completion_case, changes):
    case = completion_case
    response = complete(case, **changes)
    assert response.status_code == 422, response.text
    assert not case.marker.exists()
    assert not (case.root / ".data/settings/preferences.json").exists()


def test_missing_mail_credentials_are_reported_not_ready(completion_case):
    case = completion_case
    response = case.client.post("/api/local-ui/configuration/read", headers=case.headers)
    assert response.status_code == 200, response.text
    readiness = response.json()["module_readiness"]["mail"]
    assert readiness["ready"] is False
    assert readiness["status"] == "not_ready"


@pytest.mark.parametrize("relative", ["config", "config/runtime-capabilities.json", ".data/settings"])
def test_linked_completion_paths_are_rejected(completion_case, monkeypatch, relative):
    case = completion_case
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == case.root / relative or real_is_symlink(path))
    assert complete(case).status_code == 403
    assert not case.marker.exists()


def test_marker_write_failure_does_not_report_completion(completion_case, monkeypatch):
    from packages.automation import latest_report
    case = completion_case
    original = latest_report.write_json_atomic

    def failing_marker(path, payload):
        if path == case.marker:
            raise OSError("synthetic disk failure")
        original(path, payload)

    monkeypatch.setattr(latest_report, "write_json_atomic", failing_marker)
    response = complete(case)
    assert response.status_code == 503, response.text
    assert not case.marker.exists()


def test_effective_readonly_denies_completion(completion_case):
    case = completion_case
    case.settings.write_enabled = False
    assert complete(case).status_code == 403
    assert not case.marker.exists()


def test_settings_root_cannot_redirect_completion(completion_case, tmp_path):
    case = completion_case
    elsewhere = tmp_path / "unowned"
    elsewhere.mkdir()
    case.settings.agent_root = elsewhere
    assert complete(case).status_code == 403
    assert not case.marker.exists()
    assert not list(elsewhere.iterdir())


def test_atomic_replace_failure_keeps_previous_marker(completion_case, monkeypatch):
    case = completion_case
    original_marker = '{"schema":1,"instance_id":"previous","first_run_complete":false}'
    case.marker.write_text(original_marker, encoding="utf-8")
    replace = Path.replace

    def fail_marker_replace(path, target):
        if target == case.marker:
            assert case.marker.read_text(encoding="utf-8") == original_marker
            assert json.loads(path.read_text(encoding="utf-8"))["first_run_complete"] is True
            raise OSError("synthetic replacement failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_marker_replace)
    assert complete(case).status_code == 503
    assert case.marker.read_text(encoding="utf-8") == original_marker
    assert not list(case.marker.parent.glob("*.tmp"))
