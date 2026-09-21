from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

from packages.config import get_settings


def _load_main(tmp_path, monkeypatch):
    agent_root = tmp_path / "isolated-agent"
    agent_root.mkdir(exist_ok=True)
    for name in tuple(os.environ):
        if name.startswith("RECRUITOPS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RECRUITOPS_AGENT_ROOT", str(agent_root))
    get_settings.cache_clear()
    module = importlib.import_module("apps.api.main")
    get_settings.cache_clear()
    return module


class FakeStorage:
    def __init__(self):
        self.engine = SimpleNamespace(disposed=False, dispose=self.dispose)

    def dispose(self):
        self.engine.disposed = True


class FakeStore:
    def __init__(self):
        self.recovered = 0
        self.skipped = 0

    def recover_interrupted(self):
        self.recovered += 1

    def skip_missed_occurrences(self):
        self.skipped += 1


class FakeWorker:
    def __init__(self):
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stopped = False

    async def run_forever(self):
        self.started.set()
        await self.finished.wait()

    def stop(self):
        self.stopped = True
        self.finished.set()


def test_worker_lifecycle_tracks_saved_automation_toggle_without_broadening_codex(tmp_path, monkeypatch):
    main = _load_main(tmp_path, monkeypatch)
    settings = SimpleNamespace(
        codex_runtime_enabled=True,
        automation_enabled=False,
        database_url="sqlite:///:memory:",
        automation_poll_seconds=10,
        automation_run_timeout_seconds=600,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    created = []

    def build_worker(current):
        storage, store, worker = FakeStorage(), FakeStore(), FakeWorker()
        created.append((current, storage, store, worker))
        return storage, store, worker

    monkeypatch.setattr(main, "_build_automation_worker", build_worker)

    async def scenario():
        state = SimpleNamespace()
        lifecycle = main._AutomationWorkerLifecycle(state, codex_runtime_started=True)
        await lifecycle.reconcile()
        assert state.automation_worker_status == "disabled"
        assert created == []

        settings.automation_enabled = True
        await lifecycle.reconcile()
        await asyncio.sleep(0)
        assert state.automation_worker_status == "running"
        assert created[0][2].recovered == 1
        assert created[0][2].skipped == 1

        settings.automation_enabled = False
        await lifecycle.reconcile()
        assert created[0][3].stopped
        assert state.automation_worker_status == "stopping"
        await asyncio.sleep(0)
        await lifecycle.reconcile()
        assert state.automation_worker_status == "disabled"
        assert created[0][1].engine.disposed

        settings.automation_enabled = True
        await lifecycle.reconcile()
        await asyncio.sleep(0)
        assert len(created) == 2
        await lifecycle.close()
        assert created[1][3].stopped
        assert created[1][1].engine.disposed

    asyncio.run(scenario())


def test_worker_lifecycle_requires_codex_runtime_to_have_started(tmp_path, monkeypatch):
    main = _load_main(tmp_path, monkeypatch)
    settings = SimpleNamespace(
        codex_runtime_enabled=True,
        automation_enabled=True,
        database_url="sqlite:///:memory:",
        automation_poll_seconds=10,
        automation_run_timeout_seconds=600,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    built = []
    monkeypatch.setattr(main, "_build_automation_worker", lambda _: built.append(True))

    async def scenario():
        state = SimpleNamespace()
        lifecycle = main._AutomationWorkerLifecycle(state, codex_runtime_started=False)
        await lifecycle.reconcile()
        assert state.automation_worker_status == "disabled"
        assert built == []
        await lifecycle.close()

    asyncio.run(scenario())


def test_mail_only_configuration_does_not_start_automation_worker(tmp_path, monkeypatch):
    main = _load_main(tmp_path, monkeypatch)
    settings = SimpleNamespace(
        codex_runtime_enabled=False,
        automation_enabled=True,
        mail_enabled=True,
        database_url="sqlite:///:memory:",
        automation_poll_seconds=10,
        automation_run_timeout_seconds=600,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    built = []
    monkeypatch.setattr(main, "_build_automation_worker", lambda _: built.append(True))

    async def scenario():
        state = SimpleNamespace()
        lifecycle = main._AutomationWorkerLifecycle(state, codex_runtime_started=False)
        await lifecycle.reconcile()
        assert state.automation_worker_status == "disabled"
        assert built == []
        await lifecycle.close()

    asyncio.run(scenario())


def test_mail_task_is_blocked_when_its_own_capability_is_off(tmp_path, monkeypatch):
    main = _load_main(tmp_path, monkeypatch)
    settings = SimpleNamespace(
        codex_runtime_enabled=True,
        automation_enabled=True,
        mail_enabled=False,
        write_enabled=True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    class Executor:
        called = False

        async def __call__(self, _task):
            self.called = True
            raise AssertionError("disabled mail task reached executor")

    executor = Executor()
    result = asyncio.run(main._execute_automation_if_authorized(
        executor,
        SimpleNamespace(task_id="recruitment_mailbox"),
    ))

    assert result.status == "blocked"
    assert result.error == "mail_disabled"
    assert executor.called is False


def test_saved_desktop_automation_toggle_reloads_under_fixed_authorization_mask(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "isolated-agent"
    settings_dir = root / ".data" / "settings"
    settings_dir.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "runtime-capabilities.json").write_text(json.dumps({
        "schema": 1,
        "instance_id": "a" * 32,
        "first_run_complete": False,
    }), encoding="utf-8")
    preferences = settings_dir / "preferences.json"
    preferences.write_text(json.dumps({
        "automation_enabled": False,
        "codex_runtime_enabled": True,
        "llm_enabled": True,
        "llm_api_key": "synthetic-only",
        "model_name": "synthetic-model",
        "model_api_style": "anthropic",
        "model_api_base_url": "https://model.example.invalid",
        "mail_enabled": True,
        "mail_imap_host": "imap.example.invalid",
        "mail_imap_port": 993,
        "mail_imap_username": "fixture@example.invalid",
        "mail_imap_password": "synthetic-only",
        "mail_sync_on_startup": False,
    }), encoding="utf-8")

    instance_id = "a" * 32
    capability_mask = {
        "llm_enabled": True,
        "job_analysis_enabled": False,
        "codex_runtime_enabled": True,
        "mail_enabled": True,
        "mail_sync_on_startup": False,
        "automation_enabled": True,
        "vision_enabled": False,
    }
    monkeypatch.setenv("RECRUITOPS_AGENT_ROOT", str(root))
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", "true")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_LAUNCH_MODE", "packaged")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", instance_id)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", instance_id)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", json.dumps(capability_mask))
    get_settings.cache_clear()
    try:
        before = get_settings()
        assert before.automation_enabled is True
        assert before.codex_runtime_enabled is True
        assert before.mail_enabled is True

        preferences.write_text(json.dumps({
            "automation_enabled": True,
            "codex_runtime_enabled": True,
            "llm_enabled": True,
            "llm_api_key": "synthetic-only",
            "model_name": "synthetic-model",
            "model_api_style": "anthropic",
            "model_api_base_url": "https://model.example.invalid",
            "mail_enabled": True,
            "mail_imap_host": "imap.example.invalid",
            "mail_imap_port": 993,
            "mail_imap_username": "fixture@example.invalid",
            "mail_imap_password": "synthetic-only",
            "mail_sync_on_startup": False,
        }), encoding="utf-8")
        get_settings.cache_clear()

        after = get_settings()
        assert after.automation_enabled is True
        assert after.codex_runtime_enabled is True
        assert after.llm_enabled is True
        assert after.mail_enabled is True
    finally:
        get_settings.cache_clear()
