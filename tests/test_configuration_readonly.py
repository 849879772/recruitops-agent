"""Configuration reads must work without materializing first-run files."""
import pytest
import yaml

from packages.config import get_settings
from tests.test_owner_configuration import owner


@pytest.mark.parametrize("existing_profile", [False, True])
def test_readonly_configuration_returns_full_schema_without_writes(owner, monkeypatch, existing_profile):
    client, headers, fixture_root, _ = owner
    root = fixture_root / "readonly-instance"
    if existing_profile:
        (root / "config").mkdir(parents=True)
        (root / "config/candidate_profile.yaml").write_text(
            "profile: {skills: [SQL], matching: {title_keywords: [Accounting]}}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("RECRUITOPS_AGENT_ROOT", str(root))
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", "false")
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "false")
    get_settings.cache_clear()

    def unexpected_bootstrap(*args):
        raise AssertionError("read-only configuration must not bootstrap files")

    monkeypatch.setattr("apps.api.configuration.ensure_anonymous_configuration", unexpected_bootstrap)

    def snapshot():
        return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
                for path in root.rglob("*")}

    before = snapshot()
    for _ in range(2):
        response = client.post("/api/local-ui/configuration/read", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert set(payload) == {
            "settings", "secrets", "profile", "model_connections",
            "active_model_connection_id", "configured_capabilities", "options",
            "onboarding", "bootstrap", "restart_required",
            "module_readiness", "model_migration_required",
        }
        assert payload["bootstrap"] == {"companies_created": False, "profile_created": False}
        assert payload["model_connections"] and payload["options"]["industry_groups"]
        assert payload["options"]["mail_providers"]
        assert payload["profile"]["scope"]["industry_groups"]
        assert payload["profile"]["exclusions"]["internships"] == "exclude"
        assert payload["onboarding"]["ready"] is False
        assert "test-secret" not in response.text
        assert payload["profile"]["skills"] == (["SQL"] if existing_profile else [])
        assert payload["profile"]["matching"]["title_keywords"] == (
            ["Accounting"] if existing_profile else []
        )
        assert payload["onboarding"]["missing"] == (
            ["model"] if existing_profile else ["model", "resume", "title_keywords"]
        )
        assert snapshot() == before
        assert not (root / "config/companies.yaml").exists()
        if not existing_profile:
            assert not root.exists()


def test_write_optin_keeps_bootstrap_and_profile_save(owner):
    client, headers, root, _ = owner
    (root / "config/candidate_profile.yaml").unlink()
    response = client.post("/api/local-ui/configuration/read", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["bootstrap"] == {"companies_created": True, "profile_created": True}
    assert yaml.safe_load((root / "config/companies.yaml").read_text(encoding="utf-8")) == {"companies": []}
    assert (root / "config/candidate_profile.yaml").is_file()
    saved = client.post("/api/local-ui/configuration/save", headers=headers,
                        json={"profile": {"skills": ["SQL"],
                                          "matching": {"title_keywords": ["Accounting"]}}})
    assert saved.status_code == 200, saved.text
    profile = yaml.safe_load((root / ".data/settings/candidate_profile.yaml").read_text(encoding="utf-8"))
    assert profile["profile"]["matching"]["title_keywords"] == ["Accounting"]
