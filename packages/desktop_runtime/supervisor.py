"""Owned PostgreSQL lifecycle with persistent, explicitly isolated instances."""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.request
from pathlib import Path

from . import PROTOCOL_VERSION, RuntimeFailure
from .capabilities import automation_hot_reload_allowed, configured_capabilities
from .isolation import InstanceLock, PortLease, child_environment
from .instance import Instance
from .windows import WindowsTree


class Events:
    def __init__(self, stream, instance_id=None):
        self.stream = stream
        self.instance_id = instance_id
        self.run_id = secrets.token_hex(16)
        self.sequence = 0

    def emit(self, event, stage, **fields):
        self.sequence += 1
        record = {"protocol": PROTOCOL_VERSION, "sequence": self.sequence,
                  "run_id": self.run_id,
                  "event": event, "stage": stage, **fields}
        if self.instance_id is not None:
            record["instance_id"] = self.instance_id
        self.stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        self.stream.flush()


class Supervisor:
    def __init__(self, bundle, layout, repository, events, *, tree_factory=WindowsTree,
                 probe=None, sleep=time.sleep, clock=time.monotonic, timeout=30,
                 instance_factory=Instance, recover=False, enable_writes_for_instance=None,
                 desktop=False):
        self.bundle, self.layout, self.repository, self.events = bundle, layout, repository, events
        self.tree_factory, self.probe, self.sleep, self.clock = tree_factory, probe, sleep, clock
        self.timeout = timeout
        self.tree = self.lock = None
        self.leases = []
        self.services = {}
        self.stage = "preflight"
        self.env = {}
        self.last_tick = None
        self.shell_token = None
        self.instance_factory = instance_factory
        self.instance = None
        self.recover = recover
        self.enable_writes_for_instance = enable_writes_for_instance
        self.desktop = desktop
        self.writes = False
        self.failed = False
        self.stopped = False
        self.capabilities = {}

    def command(self, name, *args):
        return [str(self.bundle.resource(name)), *map(str, args)]

    def run_step(self, stage, argv):
        self.stage = stage
        self.events.emit("starting", stage)
        child = self.tree.spawn(argv, self.layout.data, self.env)
        exit_code = child.wait(self.timeout)
        if exit_code != 0:
            raise RuntimeFailure(f"{stage}_failed", exit_code=exit_code)
        self.events.emit("completed", stage)

    def db_ready(self):
        expected = (self.layout.data / "pgdata").resolve().as_posix().replace("'", "''")
        query = ("SELECT 1 / CASE WHEN replace(current_setting('data_directory'), chr(92), '/') = '"
                 + expected + "' THEN 1 ELSE 0 END")
        try:
            child = self.tree.spawn(self.command("psql", "-X", "-w", "-v", "ON_ERROR_STOP=1", "-c", query), self.layout.data, self.env)
            return child.wait(3) == 0
        except RuntimeFailure as exc:
            if exc.code == "process_timeout":
                raise
            return False

    def api_ready(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.api_port}/desktop-runtime/ready",
            headers={"Authorization": "Bearer " + self.env["RECRUITOPS_API_TOKEN"]},
        )
        try:
            # Ignore host proxy configuration even if the parent environment is hostile.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
                data = json.loads(response.read(4096))
                return data == {"instance_id": self.events.instance_id, "run_id": self.events.run_id,
                                "status": "ready", "writes": self.writes, "websocket": self.writes}
        except (OSError, ValueError):
            return False

    def await_ready(self, stage):
        self.stage = stage
        deadline = self.clock() + self.timeout
        while True:
            self.check_children()
            if (self.probe(stage) if self.probe else (self.db_ready() if stage == "database" else self.api_ready())):
                self.check_children()
                self.events.emit("ready", stage)
                return
            if self.clock() >= deadline:
                raise RuntimeFailure(f"{stage}_ready_timeout")
            self.sleep(0.2)

    def check_children(self):
        for name, child in self.services.items():
            exit_code = child.poll()
            if exit_code is not None:
                raise RuntimeFailure(f"{name}_exited", exit_code=exit_code)

    def start(self):
        try:
            self.layout.validate(self.repository)
            self.bundle.verify()
            if self.shell_token is not None and not re.fullmatch(r"[0-9a-f]{64}", self.shell_token):
                raise RuntimeFailure("invalid_shell_token")
            if self.desktop and (not self.shell_token or self.enable_writes_for_instance is not None):
                raise RuntimeFailure("invalid_desktop_launch")
            self.layout.data.mkdir(parents=True, exist_ok=True)
            self.lock = InstanceLock(self.layout.data, self.events.run_id).acquire()
            candidate = self.instance_factory(self.layout.data)
            password, fresh = candidate.open(recover=self.recover)
            self.events.instance_id = candidate.record["instance_id"]
            if self.desktop:
                self.writes = True
            elif self.enable_writes_for_instance is not None:
                if fresh or self.enable_writes_for_instance != self.events.instance_id or not self.shell_token:
                    raise RuntimeFailure("instance_write_optin_mismatch")
                self.writes = True
            self.instance = candidate
            self.instance.save("starting", run_id=self.events.run_id)
            self.events.emit("opened", "instance", fresh=fresh, recovered=self.recover)
            self.layout.prepare()
            self.tree = self.tree_factory()
            db = PortLease()
            self.leases.append(db)
            api = PortLease()
            self.leases.append(api)
            self.db_port, self.api_port = db.port, api.port
            token = secrets.token_hex(32)
            if self.shell_token is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", self.shell_token):
                    raise RuntimeFailure("invalid_shell_token")
                token = self.shell_token
            self.env = child_environment(self.layout, self.bundle, db.port, api.port, password, token)
            self.env["RECRUITOPS_DESKTOP_INSTANCE_ID"] = self.events.instance_id
            self.env["RECRUITOPS_DESKTOP_RUN_ID"] = self.events.run_id
            self.env["RECRUITOPS_WRITE_ENABLED"] = str(self.writes).lower()
            self.env["RECRUITOPS_DESKTOP_WRITE_OPTIN"] = self.events.instance_id if self.writes else ""
            self.env["RECRUITOPS_DESKTOP_LAUNCH_MODE"] = "packaged" if self.desktop else "cli"
            self.capabilities = configured_capabilities(self.layout.data, self.events.instance_id, writes=self.writes)
            for field, enabled in self.capabilities.items():
                self.env["RECRUITOPS_" + field.upper()] = str(enabled).lower()
            runtime_capabilities = dict(self.capabilities)
            if self.desktop:
                runtime_capabilities["automation_enabled"] = automation_hot_reload_allowed(
                    self.layout.data, self.events.instance_id, writes=self.writes
                )
            self.env["RECRUITOPS_DESKTOP_CAPABILITIES"] = json.dumps(runtime_capabilities)
            self.env["RECRUITOPS_CODEX_COMMAND"] = json.dumps([str(self.bundle.resource("codex")), "app-server"])
            self.events.emit("configured", "capabilities", capabilities=self.capabilities, restart_required=False)
            self.events.emit("allocated", "ports", api_port=api.port, db_port=db.port)
            pgdata = self.layout.data / "pgdata"
            pwfile = self.layout.data / "tmp/initdb-password"
            if fresh:
                try:
                    pwfile.write_text(password + "\n", encoding="ascii")
                    pwfile.chmod(0o600)
                    self.run_step("initdb", self.command("initdb", "-D", pgdata, "-U", "desktop", "--encoding=UTF8", "--locale=C", "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", f"--pwfile={pwfile}"))
                finally:
                    pwfile.unlink(missing_ok=True)
            db.close()
            self.stage = "database"
            self.events.emit("starting", self.stage)
            self.services["database"] = self.tree.spawn(self.command("postgres", "-D", pgdata, "-h", "127.0.0.1", "-p", db.port, "-c", f"data_directory={pgdata}", "-c", "unix_socket_directories=", "-c", "password_encryption=scram-sha-256"), self.layout.data, self.env)
            self.await_ready("database")
            # Preserve a logical recovery point before any application migration.
            self.backup("pre-migration")
            self.instance.save("migrating")
            self.run_step("migration", self.command("python", "-s", "-B", self.bundle.resource("migration_script"), "--migrations-dir", self.bundle.root / "app/migrations"))
            self.check_children()
            api.close()
            self.stage = "api"
            self.events.emit("starting", self.stage)
            self.services["api"] = self.tree.spawn(self.command("python", "-s", "-B", self.bundle.resource("api_bootstrap")), self.layout.data, self.env)
            self.await_ready("api")
            if not self.capabilities["codex_runtime_enabled"]:
                self.events.emit("disabled", "agent", code="explicit_configuration_required")
            self.events.emit("disabled", "browser", code="shell_owns_browser")
            self.stage = "runtime"
            self.instance.save("ready")
            self.events.emit("ready", "runtime", api_url=f"http://127.0.0.1:{api.port}", writes=self.writes,
                             websocket=self.writes, agent=self.capabilities["codex_runtime_enabled"], browser=False,
                             capabilities=self.capabilities)
            self.last_tick = self.clock()
        except BaseException:
            self.failed = True
            self.stop()
            raise

    def backup(self, label="manual"):
        self.check_children()
        previous = self.stage
        name = f"{label}-{self.events.run_id}-{secrets.token_hex(4)}.dump"
        target = self.layout.data / "backups" / name
        partial = target.with_suffix(".partial")
        self.run_step("backup", self.command("pg_dump", "-w", "-Fc", "-f", partial))
        if not partial.is_file() or not partial.stat().st_size:
            raise RuntimeFailure("backup_empty")
        partial.replace(target)
        self.events.emit("saved", "backup", filename=name)
        self.stage = previous

    def tick(self):
        now = self.clock()
        if self.last_tick is not None and now - self.last_tick > 30:
            self.events.emit("recheck", "runtime", code="wake_or_heartbeat_gap")
            self.await_ready("database")
            self.await_ready("api")
            self.stage = "runtime"
        self.check_children()
        self.last_tick = now
        self.events.emit("heartbeat", "runtime", scheduling=self.capabilities.get("automation_enabled", False))

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        graceful = False
        try:
            if self.tree:
                tree = self.tree
                self.tree = None
                try:
                    # API termination uses its retained process handle, never a PID lookup.
                    api = self.services.get("api")
                    if api and api.poll() is None:
                        api.terminate()
                        api.wait(5)
                    database = self.services.get("database")
                    if database and database.poll() is None:
                        # pg_ctl kill uses the PID of our still-live, retained HANDLE.
                        # Unlike pg_ctl stop, it never reads an editable postmaster.pid.
                        child = tree.spawn(self.command("pg_ctl", "kill", "INT", database.pid), self.layout.data, self.env)
                        if child.wait(5) == 0:
                            graceful = database.wait(max(5, self.timeout)) == 0
                except RuntimeFailure:
                    graceful = False
                finally:
                    try:
                        tree.close()
                    except RuntimeFailure:
                        graceful = False
                        raise
        finally:
            for lease in self.leases:
                lease.close()
            self.leases.clear()
            try:
                if self.instance:
                    state = "failed" if self.failed else ("stopped" if graceful else "crashed")
                    self.instance.save(state)
            finally:
                if self.lock:
                    self.lock.close()
                    self.lock = None
            self.events.emit("stopped", "runtime", scheduling=False, graceful_database=graceful)
