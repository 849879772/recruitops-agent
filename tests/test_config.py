from pathlib import Path

import pytest

from packages.config import Settings


def test_mail_is_disabled_and_unconfigured_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.mail_enabled is False
    assert settings.mail_imap_host == "imap.163.com"
    assert settings.mail_imap_username == ""
    assert settings.mail_imap_password == ""
    assert settings.mail_imap_mailbox == "INBOX"


def test_mail_defaults_on_after_complete_credentials_are_present() -> None:
    settings = Settings(
        _env_file=None,
        mail_imap_host="imap.example.test",
        mail_imap_port=993,
        mail_imap_username="fixture@example.test",
        mail_imap_password="synthetic-only",
    )

    assert settings.mail_enabled is True


def test_job_analysis_defaults_on_but_waits_for_an_enabled_model() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_enabled is False
    assert settings.job_analysis_enabled is True
    assert settings.write_enabled is False
    assert "5433" not in settings.database_url
    assert settings.source_root != Path("D:/秋招系统")
    assert settings.offerbiu_industry_groups == [
        "internet-tech", "manufacturing-equipment", "auto-transport-equipment",
    ]


def test_chat_model_can_be_enabled_without_job_analysis(monkeypatch) -> None:
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "true")
    monkeypatch.setenv("RECRUITOPS_JOB_ANALYSIS_ENABLED", "false")

    settings = Settings(_env_file=None)

    assert settings.llm_enabled is True
    assert settings.job_analysis_enabled is False


def test_mail_settings_are_read_from_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("RECRUITOPS_MAIL_ENABLED", "true")
    monkeypatch.setenv("RECRUITOPS_MAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("RECRUITOPS_MAIL_IMAP_USERNAME", "candidate@example.com")
    monkeypatch.setenv("RECRUITOPS_MAIL_IMAP_PASSWORD", "local-secret")

    settings = Settings(_env_file=None)

    assert settings.mail_enabled is True
    assert settings.mail_imap_host == "imap.example.com"
    assert settings.mail_imap_username == "candidate@example.com"
    assert settings.mail_imap_password == "local-secret"


def test_codex_runtime_is_pinned_and_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.codex_runtime_enabled is False
    assert settings.codex_cli_version == "0.149.0"
    assert settings.codex_command == ("codex", "app-server")
    assert settings.codex_model_provider_id == "deepseek"
    assert settings.codex_model_base_url == "https://api.deepseek.com"
    assert settings.codex_model == "deepseek-flash"


def test_codex_command_is_read_as_a_json_list(monkeypatch) -> None:
    monkeypatch.setenv(
        "RECRUITOPS_CODEX_COMMAND",
        '["D:/tools/codex.cmd", "app-server"]',
    )

    settings = Settings(_env_file=None)

    assert settings.codex_command == ("D:/tools/codex.cmd", "app-server")


def test_offerbiu_industry_groups_are_normalized_and_unknown_codes_rejected() -> None:
    settings = Settings(
        _env_file=None,
        offerbiu_industry_groups="finance, finance, biotech-healthcare",
    )
    assert settings.offerbiu_industry_groups == ["finance", "biotech-healthcare"]
    with pytest.raises(ValueError, match="unsupported OfferBiu industry group"):
        Settings(_env_file=None, offerbiu_industry_groups=["developer-default"])
