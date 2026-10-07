"""Owned PostgreSQL lifecycle with persistent, explicitly isolated instances."""

from __future__ import annotations

import json
import re
import secrets
import stat
import time
import urllib.request
from pathlib import Path

from . import PROTOCOL_VERSION, RuntimeFailure
from .capabilities import automation_hot_reload_allowed, configured_capabilities
from .isolation import InstanceLock, PortLease, child_environment
from .instance import Instance, secure_directory
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
                 desktop=False, backup_timeout=600):
        self.bundle, self.layout, self.repository, self.events = bundle, layout, repository, events
        self.tree_factory, self.probe, self.sleep, self.clock = tree_factory, probe, sleep, clock
        self.timeout = timeout
        self.backup_timeout = backup_timeout
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
        self.incomplete_backups = set()
        self.current_backup = None

    def command(self, name, *args):
        return [str(self.bundle.resource(name)), *map(str, args)]

    def run_step(self, stage, argv, *, output=None, timeout=None, success_codes=(0,), progress=False):
        self.stage = stage
        fields = {"log": str(Path(output).relative_to(self.layout.data)).replace("\\", "/")} if output else {}
        self.events.emit("starting", stage, elapsed_seconds=0, **fields)
        started = self.clock()
        limit = self.timeout if timeout is None else timeout
        child = self.tree.spawn(argv, self.layout.data, self.env, output=output)
        try:
            if progress:
                next_progress = started + 5
                while (exit_code := child.poll()) is None:
                    self.check_children()
                    now = self.clock()
                    if now - started >= limit:
                        raise RuntimeFailure("process_timeout")
                    if now >= next_progress:
                        self.events.emit("progress", stage, elapsed_seconds=round(now - started, 1))
                        next_progress = now + 5
                    self.sleep(0.2)
            else:
                exit_code = child.wait(limit)
        except RuntimeFailure as exc:
            if exc.code != "process_timeout":
                raise
            # Reap the owned handle before a failed backup can be removed.
            try:
                child.terminate()
                child.wait(5)
            except RuntimeFailure:
                pass  # stop() still owns and closes the entire process tree.
            self.events.emit("timed_out", stage, elapsed_seconds=round(self.clock() - started, 1))
            raise RuntimeFailure(f"{stage}_timeout") from exc
        if exit_code not in success_codes:
            raise RuntimeFailure(f"{stage}_failed", exit_code=exit_code)
        self.events.emit("completed", stage, elapsed_seconds=round(self.clock() - started, 1))
        return exit_code

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
            if self.layout.data.exists():
                self.lock = InstanceLock(self.layout.data, self.events.run_id).acquire()
                secure_directory(self.layout.data)
            else:
                secure_directory(self.layout.data, create=True)
                self.lock = InstanceLock(self.layout.data, self.events.run_id).acquire()
            candidate = self.instance_factory(self.layout.data)
            password, fresh = candidate.open(recover=self.recover or self.desktop)
            self.events.instance_id = candidate.record["instance_id"]
            if self.desktop:
                self.writes = True
            elif self.enable_writes_for_instance is not None:
                if fresh or self.enable_writes_for_instance != self.events.instance_id or not self.shell_token:
                    raise RuntimeFailure("instance_write_optin_mismatch")
                self.writes = True
            self.instance = candidate
            self.instance.save("starting", run_id=self.events.run_id)
            self.events.emit("opened", "instance", fresh=fresh,
                             recovered=bool(getattr(candidate, "recovered", False)))
            self.layout.prepare()
            # The instance lock excludes another supervisor; these are leftovers
            # from processes owned by an earlier, already-closed Windows job.
            self.cleanup_partial_backups()
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
                    self.run_step("initdb", self.command("initdb", "-D", pgdata, "-U", "desktop", "--encoding=UTF8", "--locale=C", "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", f"--pwfile={pwfile}"),
                                  output=self.layout.data / "logs/initdb.log")
                finally:
                    pwfile.unlink(missing_ok=True)
            db.close()
            self.stage = "database"
            database_log = self.layout.data / "logs/postgres.log"
            self.events.emit("starting", self.stage, log="logs/postgres.log")
            self.services["database"] = self.tree.spawn(self.command("postgres", "-D", pgdata, "-h", "127.0.0.1", "-p", db.port, "-c", f"data_directory={pgdata}", "-c", "unix_socket_directories=", "-c", "password_encryption=scram-sha-256"), self.layout.data, self.env, output=database_log)
            self.await_ready("database")
            migration = self.command("python", "-s", "-B", self.bundle.resource("migration_script"),
                                     "--migrations-dir", self.bundle.root / "app/migrations")
            pending = self.run_step("migration_check", [*migration, "--check-only"],
                                    output=self.layout.data / "logs/migration-check.log", success_codes=(0, 10)) == 10
            if pending:
                # Only a newly initialized cluster is known to contain no user data.
                # An existing database without a migration ledger still needs backup.
                if not fresh:
                    self.backup("pre-migration")
                self.instance.save("migrating")
                self.run_step("migration", migration, timeout=max(120, self.timeout),
                              output=self.layout.data / "logs/migration.log")
            else:
                self.events.emit("skipped", "migration", reason="up_to_date")
            self.prune_automatic_backups()
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
        if label not in {"manual", "pre-migration"}:
            raise RuntimeFailure("invalid_backup_label")
        self.check_children()
        previous = self.stage
        name = f"{label}-{self.events.run_id}-{secrets.token_hex(4)}.dump"
        target = self.layout.data / "backups" / name
        partial = target.with_suffix(".partial")
        self.incomplete_backups.add(partial)
        self.run_step("backup", self.command("pg_dump", "-w", "-Fc", "-f", partial),
                      output=self.layout.data / "logs/backup.log", timeout=self.backup_timeout, progress=True)
        if not partial.is_file() or not partial.stat().st_size:
            raise RuntimeFailure("backup_empty")
        self.verify_backup(partial)
        partial.replace(target)
        self.incomplete_backups.discard(partial)
        self.current_backup = target
        self.events.emit("saved", "backup", filename=name)
        if label == "pre-migration":
            self.prune_automatic_backups(verified=target)
        self.stage = previous

    def verify_backup(self, path):
        # This validates the archive catalogue, not a full restore of its data.
        self.run_step("backup_verify", self.command("pg_restore", "--list", path),
                      output=self.layout.data / "logs/backup-verify.log", timeout=max(30, self.timeout))

    def backup_files(self, pattern):
        directory = self.layout.data / "backups"
        files = []
        for path in directory.iterdir():
            if not re.fullmatch(pattern, path.name):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            files.append((path, info.st_mtime_ns))
        return [path for path, _ in sorted(files, key=lambda item: (item[1], item[0].name), reverse=True)]

    def remove_backup_files(self, paths):
        removed = 0
        for path in paths:
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                self.events.emit("warning", "backup_cleanup", code="backup_cleanup_deferred")
        if removed:
            self.events.emit("cleaned", "backup_cleanup", removed=removed)

    def cleanup_partial_backups(self):
        try:
            paths = self.backup_files(r"(?:pre-migration|manual)-[0-9a-f]{32}-[0-9a-f]{8}\.partial")
            self.remove_backup_files(paths)
        except OSError:
            self.events.emit("warning", "backup_cleanup", code="backup_cleanup_deferred")

    def prune_automatic_backups(self, *, verified=None):
        previous = self.stage
        try:
            files = self.backup_files(r"pre-migration-[0-9a-f]{32}-[0-9a-f]{8}\.dump")
            verified = verified or self.current_backup
            if verified in files:
                files.remove(verified)
                files.insert(0, verified)
            if len(files) <= 3:
                return
            # Validate all retained recovery points before pruning legacy backups.
            # A corrupt recent archive must never cause deletion of older good ones.
            for path in files[:3]:
                if path != verified:
                    self.verify_backup(path)
            self.remove_backup_files(files[3:])
        except (RuntimeFailure, OSError):
            self.events.emit("warning", "backup_cleanup", code="backup_retention_deferred")
        finally:
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
                        self.remove_backup_files(self.incomplete_backups)
                        self.incomplete_backups.clear()
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
