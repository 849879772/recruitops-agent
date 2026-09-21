"""Synthetic saved mailbox capability gates; no IMAP connections or user data."""

import json
import os

import pytest

from packages.config import get_settings
from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.capabilities import configured_capabilities, saved_mail_configured


INSTANCE = "a" * 32
MAIL = {"mail_enabled": True, "mail_sync_on_startup": True,
        "mail_imap_host": "imap.example.test", "mail_imap_port": 993,
        "mail_imap_username": "synthetic@example.test", "mail_imap_password": "synthetic-only"}


@pytest.fixture
def desktop_mail(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("RECRUITOPS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "ENV": "desktop-isolated", "AGENT_ROOT": str(tmp_path),
        "SOURCE_ROOT": str(tmp_path / "absent-source"), "WRITE_ENABLED": "true",
        "DESKTOP_LAUNCH_MODE": "packaged", "DESKTOP_INSTANCE_ID": INSTANCE,
        "DESKTOP_WRITE_OPTIN": INSTANCE, "DESKTOP_CAPABILITIES": "{}",
    }.items():
        monkeypatch.setenv("RECRUITOPS_" + name, value)
    directory = tmp_path / ".data/settings"
    directory.mkdir(parents=True)

    def save(values):
        (directory / "preferences.json").write_text(json.dumps(values), encoding="utf-8")
        try:
            mask = configured_capabilities(tmp_path, INSTANCE, writes=True)
        except RuntimeFailure:
            mask = {}
        monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", json.dumps(mask))
        get_settings.cache_clear()

    save(MAIL)
    yield tmp_path, save
    get_settings.cache_clear()


@pytest.mark.parametrize("sync", [True, False, None])
def test_mail_only_works_without_profile_model_or_marker(desktop_mail, sync):
    root, save = desktop_mail
    values = {**MAIL, "automation_enabled": True, "job_analysis_enabled": True}
    if sync is None:
        values.pop("mail_sync_on_startup")
    else:
        values["mail_sync_on_startup"] = sync
    save(values)
    startup = configured_capabilities(root, INSTANCE, writes=True)
    settings = get_settings()
    assert startup["mail_enabled"] and settings.mail_enabled
    assert startup["mail_sync_on_startup"] is (sync is True)
    assert settings.mail_sync_on_startup is (sync is True)
    assert not settings.llm_enabled and not settings.codex_runtime_enabled
    assert not settings.job_analysis_enabled and not settings.automation_enabled
    assert not startup["job_analysis_enabled"] and not startup["automation_enabled"]
    assert not (root / "config").exists()
    assert not (root / ".data/settings/candidate_profile.yaml").exists()


@pytest.mark.parametrize("field", ["mail_imap_host", "mail_imap_port", "mail_imap_username", "mail_imap_password"])
def test_missing_saved_mail_never_uses_ambient_credentials(desktop_mail, monkeypatch, field):
    root, save = desktop_mail
    values = dict(MAIL)
    values.pop(field)
    monkeypatch.setenv("RECRUITOPS_" + field.upper(), str(MAIL[field]))
    save(values)
    assert not configured_capabilities(root, INSTANCE, writes=True)["mail_enabled"]
    assert not get_settings().mail_enabled and not get_settings().mail_sync_on_startup


@pytest.mark.parametrize("host", ["", " ", "https://imap.example.test", "imap.example.test:993",
                                 "user@imap.example.test", "imap.example.test/path", "bad host",
                                 "bad..host", "-bad.example", "999.999.999.999"])
def test_invalid_host_disables_even_stale_mail_mask(desktop_mail, monkeypatch, host):
    root, save = desktop_mail
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", '{"mail_enabled":true,"mail_sync_on_startup":true}')
    save({**MAIL, "mail_imap_host": host})
    assert not configured_capabilities(root, INSTANCE, writes=True)["mail_enabled"]
    assert not get_settings().mail_enabled and not get_settings().mail_sync_on_startup


@pytest.mark.parametrize("name,value", [("WRITE_ENABLED", "false"), ("DESKTOP_LAUNCH_MODE", "cli"),
                                       ("DESKTOP_WRITE_OPTIN", "b" * 32), ("DESKTOP_INSTANCE_ID", "bad")])
def test_mail_requires_owner_packaged_identity_even_with_true_mask(desktop_mail, monkeypatch, name, value):
    _, _save = desktop_mail
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", '{"mail_enabled":true,"mail_sync_on_startup":true}')
    monkeypatch.setenv("RECRUITOPS_" + name, value)
    settings = get_settings()
    assert not settings.mail_enabled and not settings.mail_sync_on_startup


def test_readonly_startup_and_legacy_false_mail_default(desktop_mail, monkeypatch):
    root, save = desktop_mail
    assert not any(configured_capabilities(root, INSTANCE, writes=False).values())
    save({**MAIL, "mail_enabled": False})
    capabilities = configured_capabilities(root, INSTANCE, writes=True)
    assert capabilities["mail_enabled"]
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", json.dumps(capabilities))
    get_settings.cache_clear()
    assert get_settings().mail_enabled and get_settings().mail_sync_on_startup


def test_saved_mail_changes_recompute_after_cache_reload(desktop_mail):
    _, save = desktop_mail
    save({})
    assert not get_settings().mail_enabled
    save(MAIL)
    assert get_settings().mail_enabled
    save({**MAIL, "mail_sync_on_startup": False})
    assert get_settings().mail_enabled and not get_settings().mail_sync_on_startup


@pytest.mark.parametrize("port", [None, True, "993", 0, -1, 65536])
def test_saved_mail_port_is_strict(port):
    assert not saved_mail_configured({**MAIL, "mail_imap_port": port})


@pytest.mark.parametrize("port", [0, 65536, True, "993"])
def test_settings_reject_invalid_or_coerced_saved_port(desktop_mail, port):
    root, save = desktop_mail
    save({**MAIL, "mail_imap_port": port})
    assert not configured_capabilities(root, INSTANCE, writes=True)["mail_enabled"]
    assert not get_settings().mail_enabled and not get_settings().mail_sync_on_startup


@pytest.mark.parametrize("host", ["imap.example.test", "127.0.0.1", "::1"])
def test_saved_mail_hostname_or_ip(host):
    assert saved_mail_configured({**MAIL, "mail_imap_host": host})


@pytest.mark.parametrize("field", ["mail_enabled", "mail_sync_on_startup"])
def test_settings_never_treat_string_true_as_saved_optin(desktop_mail, field):
    _, save = desktop_mail
    save({**MAIL, field: "true"})
    settings = get_settings()
    assert not getattr(settings, field)
