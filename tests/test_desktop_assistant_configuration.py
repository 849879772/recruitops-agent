import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.config import get_settings
from packages.scheduler import TaskContext, TaskType, build_runtime_task_handlers
from tests.test_owner_configuration import owner


MODEL = {"id": "synthetic", "name": "Synthetic", "provider": "deepseek",
         "api_style": "anthropic", "base_url": "https://api.deepseek.com",
         "model": "deepseek-flash", "api_key": "fixture-not-a-real-key"}
URL = "/api/local-ui/configuration"


def desktop(monkeypatch, *, mode="packaged", optin=True):
    instance = "b" * 32
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_LAUNCH_MODE", mode)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", instance)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", instance if optin else "wrong")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", "{}")
    get_settings.cache_clear()


def save(client, headers, **extra):
    return client.post(URL + "/save", headers=headers, json={
        "model_connections": [MODEL], "active_model_connection_id": MODEL["id"], **extra})


def test_model_only_save_enables_default_modules_without_profile_or_marker(owner, monkeypatch):
    client, headers, root, _ = owner
    desktop(monkeypatch)
    (root / "config/candidate_profile.yaml").unlink()
    response = save(client, headers)
    assert response.status_code == 200, response.text
    settings = get_settings()
    from packages.desktop_runtime.capabilities import configured_capabilities
    startup = configured_capabilities(root, "b" * 32, writes=True)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", json.dumps(startup))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_enabled and settings.codex_runtime_enabled
    assert not settings.mail_enabled
    assert settings.automation_enabled and settings.job_analysis_enabled
    assert not (root / "config/runtime-capabilities.json").exists()
    assert not settings.candidate_profile_config.exists()
    assert startup["llm_enabled"] and startup["codex_runtime_enabled"]
    assert not startup["mail_enabled"]
    assert startup["automation_enabled"] and startup["job_analysis_enabled"]
    payload = client.post(URL + "/read", headers=headers).json()
    assert payload["module_readiness"]["assistant"]["status"] == "configured"
    assert payload["module_readiness"]["job_scoring"]["ready"] is True
    assert payload["module_readiness"]["scheduled_tasks"]["ready"] is True
    assert payload["module_readiness"]["mail"]["status"] == "not_ready"
    assert "IMAP" in payload["module_readiness"]["mail"]["message"]
    assert not payload["module_readiness"]["assistant"]["runtime_checked"]
    assert payload["module_readiness"]["discovery"]["missing"] == ["title_keywords"]
    assert "fixture-not-a-real-key" not in json.dumps(payload)

    class Service:
        async def thread_start(self, **kwargs):
            return {"id": "synthetic-chat"}
    monkeypatch.setattr("apps.api.main.get_codex_bff_service", lambda: Service())
    response = client.post("/api/codex/threads", json={})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("mode,optin", [("cli", True), ("packaged", False)])
def test_untrusted_or_stale_launch_reports_restart_not_missing_model(owner, monkeypatch, mode, optin):
    client, headers, _, _ = owner
    desktop(monkeypatch, mode=mode, optin=optin)
    assert save(client, headers).status_code == 200
    payload = client.post(URL + "/read", headers=headers).json()
    assistant = payload["module_readiness"]["assistant"]
    assert assistant["configured"] and assistant["restart_required"]
    assert assistant["status"] == "restart_required" and not assistant["enabled"]


def test_explicit_disable_survives_later_model_save(owner, monkeypatch):
    client, headers, _, _ = owner
    desktop(monkeypatch)
    assert save(client, headers, settings={"llm_enabled": False, "codex_runtime_enabled": False}).status_code == 200
    assert save(client, headers).status_code == 200
    assert not get_settings().llm_enabled and not get_settings().codex_runtime_enabled
    assert client.post(URL + "/read", headers=headers).json()["module_readiness"]["assistant"]["status"] == "disabled"


def test_non_desktop_model_save_does_not_enable_assistant(owner, monkeypatch):
    client, headers, _, _ = owner
    monkeypatch.setenv("RECRUITOPS_ENV", "test")
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "false")
    monkeypatch.setenv("RECRUITOPS_CODEX_RUNTIME_ENABLED", "false")
    get_settings.cache_clear()
    assert save(client, headers).status_code == 200
    assert not get_settings().llm_enabled and not get_settings().codex_runtime_enabled


def test_missing_model_and_runtime_failure_are_distinct(owner, monkeypatch):
    client, headers, _, _ = owner
    desktop(monkeypatch)
    monkeypatch.setenv("RECRUITOPS_LLM_API_KEY", "")
    get_settings.cache_clear()
    assert client.post(URL + "/read", headers=headers).json()["module_readiness"]["assistant"]["status"] == "missing_model"
    assert save(client, headers).status_code == 200
    class Service:
        async def health(self):
            return SimpleNamespace(model_dump=lambda **kwargs: {
                "ready": False, "state": "failed", "detail": "synthetic startup failure"})
    monkeypatch.setattr("apps.api.main.get_codex_bff_service", lambda: Service())
    health = client.get("/api/codex/health").json()
    assert health["enabled"] and not health["ready"] and health["state"] == "failed"
    assert client.post(URL + "/read", headers=headers).json()["module_readiness"]["assistant"]["status"] == "configured"


def test_legacy_false_defaults_do_not_block_credentialed_runtime(owner, monkeypatch):
    client, headers, root, _ = owner
    desktop(monkeypatch)
    assert save(client, headers).status_code == 200
    preferences_path = root / ".data/settings/preferences.json"
    preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    preferences.update({
        "job_analysis_enabled": False,
        "automation_enabled": False,
        "mail_enabled": False,
        "mail_imap_host": "imap.example.test",
        "mail_imap_port": 993,
        "mail_imap_username": "fixture@example.test",
        "mail_imap_password": "synthetic-mail-secret",
    })
    preferences_path.write_text(json.dumps(preferences), encoding="utf-8")
    get_settings.cache_clear()
    saved = client.post(URL + "/save", headers=headers, json={
        "settings": {"offerbiu_industry_groups": ["internet-tech"]},
    })
    assert saved.status_code == 200, saved.text
    preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    assert preferences["job_analysis_enabled"] is True
    assert preferences["automation_enabled"] is True
    assert preferences["mail_enabled"] is True

    from packages.desktop_runtime.capabilities import configured_capabilities

    capabilities = configured_capabilities(root, "b" * 32, writes=True)
    assert capabilities["job_analysis_enabled"]
    assert capabilities["automation_enabled"]
    assert capabilities["mail_enabled"]
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", json.dumps(capabilities))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.job_analysis_enabled and settings.automation_enabled and settings.mail_enabled

    payload = client.post(URL + "/read", headers=headers).json()
    assert payload["module_readiness"]["job_scoring"]["ready"]
    assert payload["module_readiness"]["scheduled_tasks"]["ready"]
    assert payload["module_readiness"]["mail"]["ready"]


def test_packaged_mode_never_activates_ambient_credentials(owner, monkeypatch):
    client, headers, _, _ = owner
    desktop(monkeypatch)
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "true")
    monkeypatch.setenv("RECRUITOPS_CODEX_RUNTIME_ENABLED", "true")
    get_settings.cache_clear()
    assert not get_settings().llm_enabled and not get_settings().codex_runtime_enabled
    assert client.post(URL + "/read", headers=headers).json()["module_readiness"]["assistant"]["status"] == "missing_model"


@pytest.mark.parametrize("missing", ["title_keywords", "industry_groups"])
def test_model_only_does_not_unlock_crawling(owner, monkeypatch, missing):
    client, headers, root, _ = owner
    desktop(monkeypatch)
    profile = {"matching": {"title_keywords": [] if missing == "title_keywords" else ["Accounting"]}}
    response = save(client, headers, profile=profile)
    if missing == "title_keywords":
        assert response.status_code == 422
        assert "岗位筛选关键词不能为空" in response.json()["detail"]
        assert save(client, headers).status_code == 200
    else:
        assert response.status_code == 200
    settings = get_settings()
    if missing == "industry_groups":
        settings = settings.model_copy(update={"offerbiu_industry_groups": []})
    def forbidden(*args, **kwargs):
        raise AssertionError("missing discovery prerequisites must not start crawling")
    monkeypatch.setattr("packages.scheduler.runtime.OfferBiuRefreshService", forbidden)
    monkeypatch.setattr("packages.scheduler.runtime.DailyRecruitmentPipeline", forbidden)
    handler = build_runtime_task_handlers(settings=settings)[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value]
    result = handler(TaskContext(task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="fixture", scheduled_for=datetime(2026, 9, 18, tzinfo=timezone.utc),
        run_id="synthetic", attempt=1, write_enabled=True, metadata={"details": {"mode": "full"}}))
    assert result["status"] == "configuration_required" and missing in result["missing"]
    assert not result["agent_write_performed"]
    assert not (root / ".data/runtime").exists()
