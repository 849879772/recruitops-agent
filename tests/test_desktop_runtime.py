from __future__ import annotations

import hashlib
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import time
import types

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.__main__ import main
from packages.desktop_runtime.api_bootstrap import ReadOnlyGuard
from packages.desktop_runtime.isolation import InstanceLock, PortLease, child_environment
from packages.desktop_runtime.resources import Bundle, Layout, REQUIRED
from packages.desktop_runtime.supervisor import Events, Supervisor
from packages.desktop_runtime.windows import WindowsTree


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "install space \u4e2d\u6587"
    root.mkdir()
    entries = {name: f"bin/{name}.exe" for name in REQUIRED}
    for name in ("postgres", "initdb", "psql", "pg_dump", "pg_restore", "pg_ctl", "pg_config"):
        entries[name] = f"postgres/bin/{name}.exe"
    entries.update(vector_dll="postgres/lib/vector.dll",
                   vector_control="postgres/share/extension/vector.control",
                   vector_sql="postgres/share/extension/vector--0.8.0.sql",
                   api_bootstrap="app/packages/desktop_runtime/api_bootstrap.py",
                   migration_script="app/scripts/apply_migrations.py")
    files = {}
    for relative in (*entries.values(), "licenses/NOTICE", "app/migrations/001_test.sql"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = b"fixture only"
        if path.suffix in {".exe", ".dll"}:
            content = b"MZ" + bytes(58) + struct.pack("<I", 64) + b"PE\0\0" + struct.pack("<H", 0x8664)
        elif path.name == "vector.control":
            content = b"default_version = '0.8.0'\n"
        path.write_bytes(content)
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    components = {name: {"version": "1.0.0", "source": "https://example.invalid/fixture", "license_file": "licenses/NOTICE"}
                  for name in ("python", "node", "codex", "chromium", "postgres", "pgvector", "application")}
    components["postgres"]["version"] = "16.1"
    components["pgvector"]["version"] = "0.8.0"
    manifest = {"schema": 1, "platform": "windows-x64", "postgres_major": 16,
                "files": files, "entrypoints": entries, "components": components}
    (root / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Bundle.load(root)


def test_bundle_hash_missing_and_extra(bundle):
    target = bundle.resource("python")
    target.write_bytes(b"tampered")
    with pytest.raises(RuntimeFailure, match="hash_mismatch"):
        bundle.verify()
    target.unlink()
    with pytest.raises(RuntimeFailure, match="missing_resource"):
        bundle.verify()


def test_untracked_dll_rejected(bundle):
    (bundle.root / "unexpected.dll").write_bytes(b"x")
    with pytest.raises(RuntimeFailure, match="untracked_resource"):
        bundle.verify()


@pytest.mark.parametrize("components", [[], {"python": []}, {"python": {"version": []}}])
def test_malformed_manifest_has_structured_failure(bundle, components):
    bundle.manifest["components"] = components
    with pytest.raises(RuntimeFailure):
        bundle.verify()


@pytest.mark.parametrize("path", ["../outside", "C:/outside", "/outside", "bin/file:stream", "bin\\x",
                                  "bin/CON", "bin/file.", "bin/file ", "bin/./file"])
def test_manifest_traversal(bundle, path):
    bundle.manifest["files"][path] = "0" * 64
    with pytest.raises(RuntimeFailure, match="unsafe_resource_path"):
        bundle.verify()


def test_layout_boundaries(tmp_path):
    repo = tmp_path / "repo"
    good = repo / ".desktop-runtime-tests" / "space \u4e2d\u6587"
    Layout(tmp_path / "bundle", good).validate(repo)
    with pytest.raises(RuntimeFailure, match="isolated_instance_required"):
        Layout(tmp_path / "bundle", tmp_path / "personal").validate(repo)
    with pytest.raises(RuntimeFailure, match="resource_data_overlap"):
        Layout(good, good / "data").validate(repo)


def test_lock_double_open_and_stale_pid_not_used(tmp_path):
    # A stale record may contain a totally unrelated PID; only the OS lock matters.
    (tmp_path / "runtime.lock").write_text('{"pid":4}')
    first = InstanceLock(tmp_path, "one").acquire()
    try:
        with pytest.raises(RuntimeFailure, match="instance_locked"):
            InstanceLock(tmp_path, "two").acquire()
    finally:
        first.close()
    again = InstanceLock(tmp_path, "three").acquire()
    again.close()
    assert (tmp_path / "runtime.lock").exists()


def test_port_conflict_and_production_rejection():
    first = PortLease()
    try:
        if first.port >= 49152:
            with pytest.raises(RuntimeFailure, match="port_in_use"):
                PortLease(first.port)
        assert first.port not in (8012, 5433)
    finally:
        first.close()
    for port in (8012, 5433, 5432, 8010):
        with pytest.raises(RuntimeFailure, match="unsafe_port"):
            PortLease(port)


def test_environment_does_not_inherit_secrets(tmp_path, bundle, monkeypatch):
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "personal-secret")
    monkeypatch.setenv("PGSERVICE", "production")
    monkeypatch.setenv("PYTHONPATH", "personal")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setenv("ProgramData", "C:/ProgramData")
    env = child_environment(Layout(bundle.root, tmp_path / "data"), bundle, 55001, 55002, "abc", "token")
    assert "personal-secret" not in str(env)
    assert not {"PGSERVICE", "HTTP_PROXY", "DEEPSEEK_API_KEY"} & env.keys()
    assert env["RECRUITOPS_WRITE_ENABLED"] == "false"
    assert env["RECRUITOPS_AUTOMATION_ENABLED"] == "false"
    assert env["SYSTEMDRIVE"] == "C:"
    assert env["PROGRAMDATA"] == "C:/ProgramData"
    assert "55001" in env["RECRUITOPS_DATABASE_URL"]
    assert env["RECRUITOPS_DATABASE_URL"].startswith("postgresql+psycopg://")


class FakeChild:
    def __init__(self, result=0, service=False):
        self.result, self.service = result, service
        self.pid = 12345

    def wait(self, timeout=30):
        if self.result == "timeout":
            raise RuntimeFailure("process_timeout")
        return self.result

    def poll(self):
        return None if self.service else self.result

    def terminate(self):
        self.service = False


class FakeTree:
    def __init__(self, fail=None):
        self.fail, self.calls, self.closed = fail, [], False

    def spawn(self, argv, cwd, env):
        self.calls.append((argv, cwd, dict(env)))
        name = Path(argv[0]).stem
        if name == "pg_dump":
            name = "backup"
        if name == "python":
            name = "api" if any("api_bootstrap" in a for a in argv) else "migration"
        service = name in {"postgres", "api"}
        if self.fail == "spawn":
            raise RuntimeFailure("process_spawn_failed")
        result = 1 if name == self.fail else 0
        if name == "initdb" and not result:
            pgdata = Path(argv[argv.index("-D") + 1])
            pgdata.mkdir()
            (pgdata / "PG_VERSION").write_text("16")
        if name == "backup" and not result:
            Path(argv[argv.index("-f") + 1]).write_bytes(b"fixture-not-a-real-dump")
        if self.fail == "timeout" and name == "initdb":
            result = "timeout"
        return FakeChild(result, service and name != self.fail)

    def close(self):
        self.closed = True


def supervisor_fixture(tmp_path, bundle, fail=None, probe=None):
    repo = tmp_path / "repo"
    data = repo / ".desktop-runtime-tests" / "instance \u4e2d\u6587"
    stream, tree = io.StringIO(), FakeTree(fail)
    supervisor = Supervisor(bundle, Layout(bundle.root, data), repo, Events(stream),
                            tree_factory=lambda: tree, probe=probe or (lambda stage: True), timeout=0)
    return supervisor, tree, stream


def test_start_order_and_exit(tmp_path, bundle):
    runtime, tree, stream = supervisor_fixture(tmp_path, bundle)
    runtime.start()
    try:
        runtime.tick()
        stages = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert [r["stage"] for r in stages if r["event"] == "starting"] == ["initdb", "database", "backup", "migration", "api"]
        assert all(call[1] == runtime.layout.data for call in tree.calls)
        assert all(call[2]["RECRUITOPS_WRITE_ENABLED"] == "false" for call in tree.calls)
        assert "--auth-host=scram-sha-256" in tree.calls[0][0]
        assert not (runtime.layout.data / "tmp/initdb-password").exists()
        assert runtime.env["PGPASSWORD"] not in stream.getvalue()
        assert runtime.env["RECRUITOPS_API_TOKEN"] not in stream.getvalue()
    finally:
        runtime.stop()
    assert tree.closed


@pytest.mark.parametrize("failure", ["initdb", "backup", "migration", "postgres", "api", "spawn", "timeout"])
def test_failure_stops_owned_tree_and_downstream(tmp_path, bundle, failure):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle, failure)
    with pytest.raises(RuntimeFailure):
        runtime.start()
    assert tree.closed
    assert runtime.lock is None
    if failure != "api":
        assert not any("api_bootstrap" in str(call[0]) for call in tree.calls)
    assert not (runtime.layout.data / "tmp/initdb-password").exists()


def test_ready_timeout(tmp_path, bundle):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle, probe=lambda stage: False)
    with pytest.raises(RuntimeFailure, match="database_ready_timeout"):
        runtime.start()
    assert tree.closed


def test_recovery_never_adopts_existing_pgdata(tmp_path, bundle):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle)
    runtime.layout.data.mkdir(parents=True)
    old = runtime.layout.data / "postmaster.pid"
    old.write_text("4")
    with pytest.raises(RuntimeFailure, match="recovery_required"):
        runtime.start()
    assert old.read_text() == "4"
    assert tree.calls == []


def test_resume_rechecks_no_schedule_replay(tmp_path, bundle):
    checks = []
    runtime, _, stream = supervisor_fixture(tmp_path, bundle, probe=lambda stage: checks.append(stage) or True)
    runtime.start()
    try:
        checks.clear()
        runtime.last_tick -= 60
        runtime.tick()
        assert checks == ["database", "api"]
        assert "wake_or_heartbeat_gap" in stream.getvalue()
        assert json.loads(stream.getvalue().splitlines()[-1])["scheduling"] is False
    finally:
        runtime.stop()


def test_cli_defaults_to_preflight_without_creating_data(bundle, capsys):
    assert main(["--resources", str(bundle.root)]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["dry_run"] is True
    assert records[-1]["started"] is False
    assert main(["--resources", str(bundle.root / "missing")]) == 2
    assert "manifest_unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("kind,method,token,status", [
    ("http", "GET", b"", 401), ("http", "POST", b"Bearer secret", 403),
    ("http", "GET", b"Bearer secret", 200), ("websocket", "GET", b"Bearer secret", 1008),
])
def test_api_read_only_guard(kind, method, token, status):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})

    async def send(message):
        sent.append(message)

    asyncio.run(ReadOnlyGuard(app, "secret")({"type": kind, "method": method, "headers": [(b"authorization", token)]}, None, send))
    assert sent[0].get("status", sent[0].get("code")) == status


def test_desktop_cli_requires_explicit_isolation_root(bundle, tmp_path, capsys):
    target = tmp_path / "not-created"
    assert main(["--resources", str(bundle.root), "--instance", str(target),
                 "--start", "--desktop"]) == 2
    assert "desktop_isolation_root_required" in capsys.readouterr().out
    assert not target.exists()


@pytest.mark.parametrize("method,path,token,host,origin,status", [
    ("POST", "/api/local-ui/configuration/read", b"Bearer secret", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 200),
    ("POST", "/api/local-ui/configuration/read", b"", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 401),
    ("POST", "/api/local-ui/configuration/read", b"Bearer secret", b"localhost:55002", b"http://127.0.0.1:55002", 403),
    ("POST", "/api/local-ui/configuration/read", b"Bearer secret", b"127.0.0.1:55002", b"http://localhost:55002", 403),
    ("PUT", "/api/local-ui/configuration/read", b"Bearer secret", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 403),
    ("POST", "/api/local-ui/configuration/read/", b"Bearer secret", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 403),
    ("POST", "/api/local-ui/configuration/save", b"Bearer secret", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 403),
    ("POST", "/api/assistant", b"Bearer secret", b"127.0.0.1:55002", b"http://127.0.0.1:55002", 403),
])
def test_read_only_configuration_exception_is_exact(method, path, token, host, origin, status):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})

    async def send(message):
        sent.append(message)

    guard = ReadOnlyGuard(app, "secret", owned_origin="http://127.0.0.1:55002")
    asyncio.run(guard({"type": "http", "method": method, "path": path,
                      "headers": [(b"authorization", token), (b"host", host),
                                  (b"origin", origin)]}, None, send))
    assert sent[0]["status"] == status


def test_wrong_architecture_is_rejected_after_valid_hash(bundle):
    path = bundle.resource("python")
    raw = path.read_bytes()[:-2] + struct.pack("<H", 0x14C)
    path.write_bytes(raw)
    bundle.manifest["files"][path.relative_to(bundle.root).as_posix()] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeFailure, match="binary_architecture_mismatch"):
        bundle.verify()


def test_shell_token_handoff_is_not_an_event(tmp_path, bundle):
    runtime, _, stream = supervisor_fixture(tmp_path, bundle)
    runtime.shell_token = "a" * 64
    runtime.start()
    try:
        assert runtime.env["RECRUITOPS_API_TOKEN"] == "a" * 64
        assert "a" * 64 not in stream.getvalue()
    finally:
        runtime.stop()


def test_ready_does_not_accept_another_instance(tmp_path, bundle, monkeypatch):
    from contextlib import nullcontext

    runtime, _, _ = supervisor_fixture(tmp_path, bundle)
    runtime.api_port = 55002
    runtime.env = {"RECRUITOPS_API_TOKEN": "secret"}

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:55002/desktop-runtime/ready"
            return nullcontext(io.BytesIO(b'{"instance_id":"foreign","status":"ready","writes":false}'))

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    assert runtime.api_ready() is False


def test_db_ready_checks_its_data_directory(tmp_path, bundle):
    runtime, tree, _ = supervisor_fixture(tmp_path, bundle)
    runtime.tree = tree
    assert runtime.db_ready() is True
    sql = tree.calls[-1][0][-1]
    assert "current_setting('data_directory')" in sql
    assert (runtime.layout.data / "pgdata").as_posix() in sql
    assert "ON_ERROR_STOP=1" in tree.calls[-1][0]


def test_bootstrap_with_fixture_api_never_imports_real_settings(monkeypatch):
    from contextlib import nullcontext
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.desktop_runtime import api_bootstrap

    app = FastAPI()
    captured, queries = {}, []

    class Connection:
        def execute(self, statement):
            queries.append(str(statement))

    class Engine:
        def connect(self):
            return nullcontext(Connection())

    fixture_main = types.ModuleType("apps.api.main")
    fixture_main.app = app
    fixture_main.get_storage_engine = Engine
    monkeypatch.setitem(sys.modules, "apps.api.main", fixture_main)
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    monkeypatch.setenv("RECRUITOPS_API_TOKEN", "fixture-secret")
    monkeypatch.setenv("RECRUITOPS_API_PORT", "55002")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", "fixture-instance")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "fixture-run")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda guarded, **kwargs: captured.update(app=guarded, kwargs=kwargs))
    api_bootstrap.main()
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["ws"] == "websockets"
    with TestClient(captured["app"], base_url="http://127.0.0.1:55002") as client:
        assert client.get("/desktop-runtime/ready").status_code == 401
        headers = {"Authorization": "Bearer fixture-secret"}
        response = client.get("/desktop-runtime/ready", headers=headers)
        assert response.json() == {"instance_id": "fixture-instance", "run_id": "fixture-run", "status": "ready", "writes": False, "websocket": False}
        assert client.post("/any-write", headers=headers).status_code == 403
        assert queries == ["SELECT 1"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object native fixture")
def test_windows_job_handles_unicode_argv_and_only_owned_subtree(tmp_path):
    root = tmp_path / "space \u4e2d\u6587"
    root.mkdir()
    marker = root / "argv.json"
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    env["PYTHONUTF8"] = "1"
    tree = WindowsTree()
    unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], cwd=root, env=env,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        code = "import pathlib,sys,json,time; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:])); time.sleep(60)"
        child = tree.spawn([sys.executable, "-I", "-c", code, str(marker), "space value", "\u4e2d\u6587"], root, env)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(marker.read_text()) == ["space value", "\u4e2d\u6587"]
        # Mutating the informational PID cannot change the owned OS handle.
        child.pid = unrelated.pid
        tree.close()
        assert unrelated.poll() is None
    finally:
        tree.close()
        unrelated.terminate()
        unrelated.wait(5)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object native fixture")
def test_windows_job_assignment_failure_never_runs_child(tmp_path, monkeypatch):
    tree = WindowsTree()
    marker = tmp_path / "must-not-exist"
    monkeypatch.setattr(tree.dll, "AssignProcessToJobObject", lambda *args: False)
    try:
        with pytest.raises(RuntimeFailure, match="job_assign_failed"):
            tree.spawn([sys.executable, "-I", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)], tmp_path, {})
        assert not marker.exists()
    finally:
        tree.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object native fixture")
def test_windows_job_reaps_grandchild(tmp_path):
    tree = WindowsTree()
    marker = tmp_path / "grandchild-heartbeat"
    grandchild_code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.05)"
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    try:
        tree.spawn([sys.executable, "-I", "-c", code, grandchild_code, str(marker)], tmp_path, env)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        tree.close()
        time.sleep(0.2)
        value = marker.read_text()
        time.sleep(0.2)
        assert marker.read_text() == value
    finally:
        tree.close()
