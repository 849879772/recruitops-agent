"""OS locks, reserved loopback ports and a non-inherited child environment."""

from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path

from . import RuntimeFailure


class InstanceLock:
    def __init__(self, root: Path, instance_id: str):
        self.path = root / "runtime.lock"
        self.instance_id = instance_id
        self.stream = None

    def acquire(self):
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeFailure("instance_locked") from exc
        self.stream.seek(1)
        self.stream.truncate()
        self.stream.write(json.dumps({"pid": os.getpid(), "instance_id": self.instance_id}).encode())
        self.stream.flush()
        return self

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        # Never unlink the lock: another process could already hold its inode.


class PortLease:
    def __init__(self, port: int = 0):
        if port and not 49152 <= port <= 65535:
            raise RuntimeFailure("unsafe_port")
        for _ in range(1 if port else 16):
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt":
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                self.socket.bind(("127.0.0.1", port or (49152 + secrets.randbelow(16384))))
                self.socket.listen(1)
                self.port = self.socket.getsockname()[1]
                return
            except OSError:
                self.socket.close()
        raise RuntimeFailure("port_in_use")

    def close(self):
        self.socket.close()


def child_environment(layout, bundle, db_port: int, api_port: int, password: str, token: str) -> dict[str, str]:
    data = layout.data.resolve()
    env = {
        k: v for k, v in os.environ.items()
        if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PROGRAMDATA"}
    }
    env.update({
        "PATH": os.pathsep.join([str(bundle.resource("python").parent), str(bundle.resource("postgres").parent), str(bundle.resource("node").parent), os.path.join(env.get("SYSTEMROOT", "C:/Windows"), "System32")]),
        "HOME": str(data / "home"), "USERPROFILE": str(data / "home"),
        "APPDATA": str(data / "home"), "LOCALAPPDATA": str(data / "home"),
        "TEMP": str(data / "tmp"), "TMP": str(data / "tmp"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PYTHONPATH": str(bundle.root / "app"), "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1", "CODEX_HOME": str(data / "codex"),
        "PLAYWRIGHT_BROWSERS_PATH": str(bundle.root / "chromium"),
        "RECRUITOPS_BROWSER_EXECUTABLE_PATH": str(bundle.resource("chromium")),
        "RECRUITOPS_BROWSER_CHANNEL": "chromium",
        "PGHOST": "127.0.0.1", "PGPORT": str(db_port), "PGDATABASE": "postgres",
        "PGUSER": "desktop", "PGPASSWORD": password, "PGCONNECT_TIMEOUT": "2",
        "RECRUITOPS_ENV": "desktop-isolated", "RECRUITOPS_AGENT_ROOT": str(data),
        "RECRUITOPS_SOURCE_ROOT": str(data / "source"),
        "RECRUITOPS_SOURCE_PYTHON_EXECUTABLE": str(bundle.resource("python")),
        "RECRUITOPS_DATABASE_URL": f"postgresql+psycopg://desktop:{password}@127.0.0.1:{db_port}/postgres",
        "RECRUITOPS_API_HOST": "127.0.0.1", "RECRUITOPS_API_PORT": str(api_port),
        "RECRUITOPS_API_TOKEN": token, "RECRUITOPS_WRITE_ENABLED": "false",
        "RECRUITOPS_AUTOMATION_ENABLED": "false", "RECRUITOPS_MAIL_ENABLED": "false",
        "RECRUITOPS_MAIL_SYNC_ON_STARTUP": "false", "RECRUITOPS_CODEX_RUNTIME_ENABLED": "false",
        "RECRUITOPS_LLM_ENABLED": "false", "RECRUITOPS_JOB_ANALYSIS_ENABLED": "false",
        "RECRUITOPS_VISION_ENABLED": "false", "RECRUITOPS_CODEX_HOME": str(data / "codex"),
        "RECRUITOPS_BACKUP_ROOT": str(data / "backups"),
        "RECRUITOPS_TRACE_PATH": str(data / "logs/traces.jsonl"),
        "RECRUITOPS_CODEX_TRACE_PATH": str(data / "logs/codex-traces.jsonl"),
    })
    return env
