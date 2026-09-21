"""Startup recovery cannot cancel work outside an explicitly owned desktop run."""
from types import SimpleNamespace

import pytest

from apps.api import main


@pytest.mark.parametrize("mode,writes,env_mode,env_writes,instance,optin,expected", [
    ("desktop-isolated", True, "desktop-isolated", "true", "fixture", "fixture", 2),
    ("development", True, "desktop-isolated", "true", "fixture", "fixture", 0),
    ("desktop-isolated", False, "desktop-isolated", "true", "fixture", "fixture", 0),
    ("desktop-isolated", True, "development", "true", "fixture", "fixture", 0),
    ("desktop-isolated", True, "desktop-isolated", "false", "fixture", "fixture", 0),
    ("desktop-isolated", True, "desktop-isolated", "true", "fixture", "other", 0),
    ("desktop-isolated", True, "desktop-isolated", "true", "", "", 0),
])
def test_recovery_requires_exact_desktop_write_authority(
    monkeypatch, mode, writes, env_mode, env_writes, instance, optin, expected,
):
    monkeypatch.setenv("RECRUITOPS_ENV", env_mode)
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", env_writes)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", instance)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", optin)
    calls = []
    storage = object()

    class Store:
        def __init__(self, actual):
            assert actual is storage
            calls.append(actual)

        def recover_interrupted_operations(self):
            return 2

    monkeypatch.setattr(main, "BrowserBridgeStore", Store)
    assert main._recover_desktop_browser_operations(
        SimpleNamespace(env=mode, write_enabled=writes), storage,
    ) == expected
    assert len(calls) == (1 if expected else 0)
