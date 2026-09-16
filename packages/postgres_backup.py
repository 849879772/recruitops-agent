from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from sqlalchemy.engine import make_url


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MANIFEST_SUFFIX = ".manifest.json"


class PostgresBackupError(RuntimeError):
    """Raised when a local PostgreSQL backup or restore cannot be verified."""


@dataclass(frozen=True)
class LocalPostgresTarget:
    host: str
    port: int
    username: str
    database: str
    password: str


CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], None]


def _target(database_url: str) -> LocalPostgresTarget:
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise PostgresBackupError("invalid PostgreSQL database URL") from exc
    if not parsed.drivername.startswith("postgresql"):
        raise PostgresBackupError("PostgreSQL backup requires a PostgreSQL URL")
    host = (parsed.host or "").casefold()
    if host not in _LOCAL_HOSTS:
        raise PostgresBackupError("PostgreSQL backup is restricted to a local host")
    if not parsed.username or not parsed.database:
        raise PostgresBackupError("PostgreSQL username and database are required")
    return LocalPostgresTarget(
        host=host,
        port=int(parsed.port or 5432),
        username=parsed.username,
        database=parsed.database,
        password=parsed.password or "",
    )


def _run(command: Sequence[str], env: Mapping[str, str], timeout: float) -> None:
    try:
        completed = subprocess.run(
            list(command),
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PostgresBackupError("PostgreSQL backup utility could not be executed") from exc
    if completed.returncode != 0:
        raise PostgresBackupError("PostgreSQL backup utility returned a failure")


def _manifest_path(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}{_MANIFEST_SUFFIX}")


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def create_postgres_backup(
    database_url: str,
    destination: Path,
    *,
    pg_dump: str = "pg_dump",
    runner: CommandRunner = _run,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    target = _target(database_url)
    archive = destination.expanduser().resolve()
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.partial")
    temporary.unlink(missing_ok=True)
    command = (
        pg_dump,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--file={temporary}",
        f"--host={target.host}",
        f"--port={target.port}",
        f"--username={target.username}",
        target.database,
    )
    env = dict(os.environ)
    env["PGPASSWORD"] = target.password
    try:
        runner(command, env, timeout_seconds)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise PostgresBackupError("pg_dump did not create a non-empty archive")
        temporary.replace(archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "format": 1,
        "kind": "recruitops-postgresql-custom",
        "archive": archive.name,
        "sha256": _digest(archive),
        "target": {
            "host": target.host,
            "port": target.port,
            "username": target.username,
            "database": target.database,
        },
    }
    _manifest_path(archive).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest


def inspect_postgres_backup(archive_path: Path) -> dict[str, Any]:
    archive = archive_path.expanduser().resolve()
    manifest_path = _manifest_path(archive)
    if not archive.is_file() or not manifest_path.is_file():
        raise PostgresBackupError("PostgreSQL archive or manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PostgresBackupError("PostgreSQL backup manifest is invalid") from exc
    if (
        manifest.get("format") != 1
        or manifest.get("kind") != "recruitops-postgresql-custom"
        or manifest.get("archive") != archive.name
        or manifest.get("sha256") != _digest(archive)
        or not isinstance(manifest.get("target"), dict)
    ):
        raise PostgresBackupError("PostgreSQL backup verification failed")
    return manifest


def restore_postgres_backup(
    database_url: str,
    archive_path: Path,
    *,
    apply: bool = False,
    pg_restore: str = "pg_restore",
    runner: CommandRunner = _run,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    target = _target(database_url)
    archive = archive_path.expanduser().resolve()
    manifest = inspect_postgres_backup(archive)
    expected = manifest["target"]
    actual = {
        "host": target.host,
        "port": target.port,
        "username": target.username,
        "database": target.database,
    }
    if expected != actual:
        raise PostgresBackupError("restore target does not match the backup manifest")
    if not apply:
        return {**manifest, "restore_status": "preview"}
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    command = (
        pg_restore,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
        f"--host={target.host}",
        f"--port={target.port}",
        f"--username={target.username}",
        f"--dbname={target.database}",
        str(archive),
    )
    env = dict(os.environ)
    env["PGPASSWORD"] = target.password
    runner(command, env, timeout_seconds)
    return {**manifest, "restore_status": "applied"}


__all__ = [
    "PostgresBackupError",
    "create_postgres_backup",
    "inspect_postgres_backup",
    "restore_postgres_backup",
]
