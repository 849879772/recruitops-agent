"""Synthetic PG lifecycle tests; no real cluster/network is used here."""

import asyncio
import io
import json
import os
import queue

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.__main__ import read_commands
from packages.desktop_runtime.api_bootstrap import ReadOnlyGuard
from packages.desktop_runtime.capabilities import CAPABILITY_FIELDS, configured_capabilities, saved_model_configured
from packages.desktop_runtime.instance import Instance, protect_secret
from packages.desktop_runtime.supervisor import Events, Supervisor
from test_desktop_runtime import bundle, supervisor_fixture


def reopen(runtime, tree, **kwargs):
    return Supervisor(runtime.bundle, runtime.layout, runtime.repository, Events(io.StringIO()),
                      tree_factory=lambda: tree, probe=lambda _: True, timeout=0, **kwargs)


def test_restart_keeps_database_password_identity_and_unique_backups(tmp_path, bundle):
    first, tree, stream = supervisor_fixture(tmp_path, bundle)
    first.start()
    marker = first.layout.data / "pgdata/fixture-data"
    marker.write_text("keep")
    password, identity = first.env["PGPASSWORD"], first.events.instance_id
    first.stop()
    calls = len(tree.calls)
    second = reopen(first, tree)
    second.start()
    try:
        assert second.events.instance_id == identity
        assert second.events.run_id != first.events.run_id
        assert second.env["PGPASSWORD"] == password
        assert marker.read_text() == "keep"
        assert not any("initdb" in call[0][0] for call in tree.calls[calls:])
        assert len(list((first.layout.data / "backups").glob("*.dump"))) == 2
        assert password not in (first.layout.data / "instance.json").read_text()
        assert password not in stream.getvalue()
    finally:
        second.stop()


def test_crash_requires_explicit_recovery_and_never_reinitializes(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.start()
    first.failed = True
    first.stop()
    with pytest.raises(RuntimeFailure, match="recovery_required_unclean_state"):
        reopen(first, tree).start()
    calls = len(tree.calls)
    recovered = reopen(first, tree, recover=True)
    recovered.start()
    recovered.stop()
    assert not any("initdb" in c[0][0] for c in tree.calls[calls:])


def test_incomplete_first_cluster_is_reinitialized_without_touching_prior_data(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle, fail="initdb")
    with pytest.raises(RuntimeFailure):
        first.start()
    count = len(tree.calls)
    tree.fail = None
    second = reopen(first, tree)
    second.start()
    second.stop()
    assert any("initdb" in call[0][0] for call in tree.calls[count:])


def test_incomplete_cluster_with_backup_still_requires_manual_recovery(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle, fail="initdb")
    with pytest.raises(RuntimeFailure):
        first.start()
    backups = first.layout.data / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "prior.dump").write_bytes(b"do-not-delete")
    count = len(tree.calls)
    with pytest.raises(RuntimeFailure, match="recovery_required_incomplete_cluster"):
        reopen(first, tree, recover=True).start()
    assert len(tree.calls) == count
    assert (backups / "prior.dump").read_bytes() == b"do-not-delete"


@pytest.mark.parametrize("identity,token", [("wrong", "a" * 64), (None, None)])
def test_write_optin_requires_exact_instance_and_shell_token(tmp_path, bundle, identity, token):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.start()
    first.stop()
    second = reopen(first, tree, enable_writes_for_instance=identity or first.events.instance_id)
    second.shell_token = token
    with pytest.raises(RuntimeFailure, match="instance_write_optin_mismatch"):
        second.start()
    assert json.loads((first.layout.data / "instance.json").read_text())["state"] == "stopped"


def test_write_optin_is_per_launch_and_not_persisted(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.start()
    first.stop()
    second = reopen(first, tree, enable_writes_for_instance=first.events.instance_id)
    second.shell_token = "a" * 64
    second.start()
    assert second.env["RECRUITOPS_WRITE_ENABLED"] == "true"
    assert second.env["RECRUITOPS_DESKTOP_WRITE_OPTIN"] == first.events.instance_id
    second.stop()
    third = reopen(second, tree)
    third.start()
    assert third.env["RECRUITOPS_WRITE_ENABLED"] == "false"
    third.stop()


def test_control_commands_and_eof():
    commands = queue.Queue()
    read_commands(io.StringIO('{"command":"backup"}\n{"command":"unknown"}\n'), commands)
    assert [commands.get_nowait() for _ in range(3)] == ["backup", "invalid", "stop"]


@pytest.mark.parametrize("line", ['{"command":[]}\n', '{"command":{}}\n', 'not-json\n', '[]\n'])
def test_malformed_control_cannot_disable_eof_shutdown(line):
    commands = queue.Queue()
    read_commands(io.StringIO(line), commands)
    assert commands.get_nowait() == "invalid"
    assert commands.get_nowait() == "stop"


def test_preflight_omits_instance_until_open():
    stream = io.StringIO()
    events = Events(stream)
    events.emit("verified", "resources")
    assert "instance_id" not in json.loads(stream.getvalue())


@pytest.mark.parametrize("kind,writes,token,expected", [
    ("http", True, b"Bearer secret", 200), ("http", True, b"", 401),
    ("websocket", True, b"Bearer secret", 200), ("websocket", True, b"", 1008),
    ("websocket", False, b"Bearer secret", 1008),
])
def test_write_and_websocket_boundary(kind, writes, token, expected):
    sent = []
    async def app(scope, receive, send):
        await send({"type": "fixture", "status": 200})
    async def send(message):
        sent.append(message)
    asyncio.run(ReadOnlyGuard(app, "secret", writes=writes)(
        {"type": kind, "method": "POST", "path": "/browser-bridge", "headers": [(b"authorization", token)]}, None, send))
    assert sent[0].get("status", sent[0].get("code")) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_dpapi_roundtrip_and_corruption():
    encrypted = protect_secret(b"synthetic-secret")
    assert b"synthetic-secret" not in encrypted
    assert protect_secret(encrypted, decrypt=True) == b"synthetic-secret"
    with pytest.raises(RuntimeFailure, match="credential_unprotect_failed"):
        protect_secret(encrypted[:10], decrypt=True)


def test_stop_is_idempotent_and_never_uses_pid_file(tmp_path, bundle):
    runtime, tree, stream = supervisor_fixture(tmp_path, bundle)
    runtime.start()
    (runtime.layout.data / "pgdata/postmaster.pid").write_text("4")
    runtime.stop()
    runtime.stop()
    commands = [call[0] for call in tree.calls if "pg_ctl" in call[0][0]]
    assert len(commands) == 1 and commands[0][-3:] == ["kill", "INT", "12345"]
    assert sum(json.loads(line)["event"] == "stopped" for line in stream.getvalue().splitlines()) == 1


@pytest.mark.parametrize("origin,host,path,expected", [
    (b"http://127.0.0.1:55002", b"127.0.0.1:55002", "/browser-bridge", 200),
    (b"http://127.0.0.1:55003", b"127.0.0.1:55002", "/browser-bridge", 1008),
    (b"https://recruitment.example", b"127.0.0.1:55002", "/browser-bridge", 1008),
    (None, b"127.0.0.1:55002", "/browser-bridge", 1008),
    (b"http://127.0.0.1:55002", b"localhost:55002", "/browser-bridge", 1008),
    (b"http://127.0.0.1:55002", b"127.0.0.1:55002", "/unknown", 1008),
])
def test_websocket_exact_owned_origin(origin, host, path, expected):
    sent = []
    async def app(scope, receive, send):
        await send({"type": "fixture", "status": 200})
    async def send(message):
        sent.append(message)
    headers = [(b"authorization", b"Bearer secret"), (b"host", host)]
    if origin:
        headers.append((b"origin", origin))
    guard = ReadOnlyGuard(app, "secret", writes=True, owned_origin="http://127.0.0.1:55002")
    asyncio.run(guard({"type": "websocket", "path": path, "headers": headers}, None, send))
    assert sent[0].get("status", sent[0].get("code")) == expected


def capability_files(root, *, marker=True, identity="fixture", **preferences):
    (root / "config").mkdir(exist_ok=True)
    (root / ".data/settings").mkdir(parents=True, exist_ok=True)
    if marker:
        (root / "config/runtime-capabilities.json").write_text(json.dumps(
            {"schema": 1, "instance_id": identity, "first_run_complete": True}))
    model = {"model_name": "synthetic-model", "model_api_base_url": "https://model.example.invalid",
             "model_api_style": "anthropic"}
    (root / ".data/settings/preferences.json").write_text(json.dumps({**model, **preferences}))


def test_assistant_independent_of_completion_but_other_capabilities_require_it(tmp_path):
    capability_files(tmp_path, marker=False, llm_enabled=True, llm_api_key="synthetic-only")
    assert configured_capabilities(tmp_path, "fixture", writes=True)["llm_enabled"]
    capability_files(tmp_path, marker=False, llm_enabled=True,
                     llm_api_key="synthetic-only", codex_runtime_enabled=True,
                     automation_enabled=True)
    assert configured_capabilities(tmp_path, "fixture", writes=True)["automation_enabled"]
    (tmp_path / "config/runtime-capabilities.json").write_text(json.dumps(
        {"schema": 1, "instance_id": "fixture", "first_run_complete": False}))
    assert configured_capabilities(tmp_path, "fixture", writes=True)["automation_enabled"]
    capability_files(tmp_path, llm_enabled=True, llm_api_key="synthetic-only", automation_enabled=True)
    assert not any(configured_capabilities(tmp_path, "fixture").values())
    enabled = configured_capabilities(tmp_path, "fixture", writes=True)
    assert enabled["llm_enabled"] and not enabled["automation_enabled"]
    assert enabled["codex_runtime_enabled"] is False
    capability_files(tmp_path, llm_enabled=True, codex_runtime_enabled=True, mail_enabled=True)
    assert not any(configured_capabilities(tmp_path, "fixture", writes=True).values())


@pytest.mark.parametrize("value", [1, "true", None])
def test_capabilities_strict_boolean(tmp_path, value):
    capability_files(tmp_path, llm_enabled=value)
    with pytest.raises(RuntimeFailure, match="invalid_instance_capabilities"):
        configured_capabilities(tmp_path, "fixture", writes=True)


def test_capability_identity_and_restart_mask(tmp_path, bundle):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle)
    runtime.start()
    runtime.stop()
    capability_files(runtime.layout.data, identity=runtime.events.instance_id,
                     llm_enabled=True, llm_api_key="synthetic-only", job_analysis_enabled=True)
    enabled = reopen(runtime, tree, enable_writes_for_instance=runtime.events.instance_id)
    enabled.shell_token = "b" * 64
    enabled.start()
    try:
        mask = json.loads(enabled.env["RECRUITOPS_DESKTOP_CAPABILITIES"])
        assert set(mask) == set(CAPABILITY_FIELDS)
        assert mask["llm_enabled"] and mask["job_analysis_enabled"]
        assert not mask["automation_enabled"]
        assert "synthetic-only" not in str(enabled.env)
    finally:
        enabled.stop()
    with pytest.raises(RuntimeFailure, match="invalid_instance_capabilities"):
        configured_capabilities(runtime.layout.data, "foreign", writes=True)


def test_automation_hot_reload_uses_default_on_for_legacy_false(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.desktop = True
    first.shell_token = "f" * 64
    first.start()
    identity = first.events.instance_id
    first.stop()

    capability_files(first.layout.data, marker=False, identity=identity,
                     llm_enabled=True, llm_api_key="synthetic-only",
                     codex_runtime_enabled=True, automation_enabled=False)
    second = reopen(first, tree, desktop=True)
    second.shell_token = "e" * 64
    second.start()
    try:
        runtime_mask = json.loads(second.env["RECRUITOPS_DESKTOP_CAPABILITIES"])
        assert second.capabilities["automation_enabled"] is True
        assert second.env["RECRUITOPS_AUTOMATION_ENABLED"] == "true"
        assert runtime_mask["automation_enabled"] is True
        assert runtime_mask["mail_enabled"] is False
        assert runtime_mask["vision_enabled"] is False
    finally:
        second.stop()


def test_desktop_fresh_and_restart_autowrite_but_cli_stays_readonly(tmp_path, bundle, monkeypatch):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.desktop = True
    first.shell_token = "c" * 64
    first.start()
    identity = first.events.instance_id
    assert first.writes and first.env["RECRUITOPS_DESKTOP_WRITE_OPTIN"] == identity
    assert first.env["RECRUITOPS_DESKTOP_LAUNCH_MODE"] == "packaged"
    assert not any(first.capabilities.values())
    first.stop()
    capability_files(first.layout.data, marker=False, llm_api_key="synthetic-only",
                     model_name="deepseek-flash", model_api_style="anthropic",
                     llm_enabled=True, codex_runtime_enabled=True,
                     mail_enabled=False, automation_enabled=False)
    second = reopen(first, tree, desktop=True)
    second.shell_token = "d" * 64
    second.start()
    try:
        assert second.events.instance_id == identity and second.writes
        assert second.capabilities["llm_enabled"] and second.capabilities["codex_runtime_enabled"]
        assert second.capabilities["job_analysis_enabled"]
        assert not second.capabilities["mail_enabled"] and second.capabilities["automation_enabled"]
        assert not (first.layout.data / "config/runtime-capabilities.json").exists()
        assert not (first.layout.data / ".data/settings/candidate_profile.yaml").exists()
        assert "synthetic-only" not in str(second.env)
    finally:
        second.stop()
    monkeypatch.setenv("RECRUITOPS_DESKTOP_LAUNCH_MODE", "packaged")
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", "true")
    third = reopen(second, tree)
    third.start()
    try:
        assert not third.writes and not any(third.capabilities.values())
        assert third.env["RECRUITOPS_DESKTOP_LAUNCH_MODE"] == "cli"
    finally:
        third.stop()


@pytest.mark.parametrize("token,optin", [(None, None), ("e" * 64, "foreign")])
def test_desktop_requires_shell_token_and_no_other_optin(tmp_path, bundle, token, optin):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle)
    runtime.desktop = True
    runtime.shell_token = token
    runtime.enable_writes_for_instance = optin
    with pytest.raises(RuntimeFailure, match="invalid_desktop_launch"):
        runtime.start()
    assert not tree.calls
    assert not (runtime.layout.data / "instance.json").exists()


@pytest.mark.parametrize("field,value", [
    ("llm_api_key", ""), ("model_name", " "), ("model_api_style", "unsupported"),
    ("model_api_base_url", "http://remote.example"),
    ("model_api_base_url", "https://user:secret@model.example"),
    ("model_api_base_url", "https://model.example?key=secret"),
    ("model_api_base_url", "https://model.example#fragment"),
])
def test_assistant_saved_model_invalid_is_disabled(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("RECRUITOPS_LLM_API_KEY", "ambient-must-not-enable")
    preferences = {"llm_api_key": "synthetic-only", "llm_enabled": True,
                   "codex_runtime_enabled": True, field: value}
    capability_files(tmp_path, marker=False, **preferences)
    assert not any(configured_capabilities(tmp_path, "fixture", writes=True).values())


def test_saved_model_helper_strict_types_and_local_endpoint():
    assert not saved_model_configured([])
    assert saved_model_configured({"llm_api_key": "synthetic", "model_name": "synthetic",
                                  "model_api_style": "openai", "model_api_base_url": "http://127.0.0.1:55003/v1"})
