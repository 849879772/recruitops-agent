import json
import pytest
from packages.config import Settings, get_settings
from packages.desktop_runtime.capabilities import saved_model_configured
from tests.test_owner_configuration import owner


@pytest.mark.parametrize("base", ["https://third-party.invalid/v1", "http://127.0.0.1:8000",
    "https://api.deepseek.com.evil.test", "https://api.deepseek.com?key=secret"])
def test_legacy_secrets_are_never_rebound_to_official_host(base):
    settings = Settings(_env_file=None, model_api_base_url=base, llm_api_key="old-secret",
                        llm_enabled=True, codex_runtime_enabled=True)
    assert settings.llm_api_key == ""
    assert not settings.llm_enabled and not settings.codex_runtime_enabled
    assert settings.model_connection_migration_required


def test_old_protocol_on_official_host_preserves_official_key():
    settings = Settings(_env_file=None, model_api_base_url="https://api.deepseek.com/v1",
                        model_api_style="openai", model_name="deepseek-flash", llm_api_key="official")
    assert settings.llm_api_key == "official"
    assert settings.model_api_style == "anthropic"
    assert not settings.model_connection_migration_required
    assert saved_model_configured({"model_api_base_url": "https://api.deepseek.com/v1",
        "model_api_style": "openai", "model_name": "deepseek-flash", "llm_api_key": "official"})


def test_assistant_cannot_pick_another_providers_environment_secret():
    settings = Settings(_env_file=None, codex_model_api_key_env="OLD_PROVIDER_KEY",
                        codex_model_provider_id="old-provider")
    assert settings.codex_model_api_key_env == "RECRUITOPS_LLM_API_KEY"
    assert settings.codex_model_provider_id == "deepseek"


def test_saved_official_connection_supersedes_legacy_environment(owner, monkeypatch):
    client, headers, root, _ = owner
    monkeypatch.setenv("RECRUITOPS_MODEL_API_BASE_URL", "https://legacy.invalid")
    directory = root / ".data/settings"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "preferences.json").write_text(json.dumps({
        "model_api_base_url": "https://api.deepseek.com", "model_name": "deepseek-flash",
        "llm_api_key": "saved-official", "llm_enabled": True}), encoding="utf-8")
    get_settings.cache_clear()
    assert not get_settings().model_connection_migration_required
    assert get_settings().llm_api_key == "saved-official"
    response = client.post("/api/local-ui/configuration/save", headers=headers,
                           json={"settings": {"vision_enabled": False}})
    assert response.status_code == 200
    assert get_settings().llm_api_key == "saved-official"


@pytest.mark.parametrize("field,value", [
    ("provider", "openai-compatible"), ("api_style", "openai"),
    ("base_url", "https://proxy.invalid"), ("model", "other-model")])
def test_configuration_rejects_retired_connections_without_network(owner, monkeypatch, field, value):
    client, headers, _, _ = owner
    monkeypatch.setattr("packages.matching.client._default_transport",
                        lambda *args: pytest.fail("must reject before network"))
    connection = {"id": "test", "provider": "deepseek", "api_style": "anthropic",
                  "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "fake",
                  field: value}
    response = client.post("/api/local-ui/configuration/model/test", headers=headers, json=connection)
    assert response.status_code == 422
    response = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "model_connections": [{"name": "test", **connection}], "active_model_connection_id": "test"})
    assert response.status_code == 422
