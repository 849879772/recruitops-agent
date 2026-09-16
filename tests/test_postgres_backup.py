from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.postgres_backup import (
    PostgresBackupError,
    create_postgres_backup,
    inspect_postgres_backup,
    restore_postgres_backup,
)


URL = "postgresql+psycopg://user:secret@127.0.0.1:5433/recruitops"


def test_backup_and_explicit_restore_keep_password_out_of_commands(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(command, env, _timeout) -> None:
        values = list(command)
        calls.append((values, dict(env)))
        output = next((item.split("=", 1)[1] for item in values if item.startswith("--file=")), None)
        if output:
            Path(output).write_bytes(b"postgres custom archive")

    archive = tmp_path / "state.dump"
    manifest = create_postgres_backup(URL, archive, runner=runner)

    assert inspect_postgres_backup(archive) == manifest
    preview = restore_postgres_backup(URL, archive, runner=runner)
    assert preview["restore_status"] == "preview"
    assert len(calls) == 1

    applied = restore_postgres_backup(URL, archive, apply=True, runner=runner)
    assert applied["restore_status"] == "applied"
    assert len(calls) == 2
    assert all("secret" not in " ".join(command) for command, _env in calls)
    assert all(env["PGPASSWORD"] == "secret" for _command, env in calls)
    assert "secret" not in json.dumps(manifest)


def test_backup_rejects_remote_database_and_tampered_archive(tmp_path: Path) -> None:
    with pytest.raises(PostgresBackupError, match="local host"):
        create_postgres_backup(
            "postgresql://user:secret@db.example.com/recruitops",
            tmp_path / "remote.dump",
        )

    def runner(command, _env, _timeout) -> None:
        output = next(item.split("=", 1)[1] for item in command if item.startswith("--file="))
        Path(output).write_bytes(b"valid")

    archive = tmp_path / "state.dump"
    create_postgres_backup(URL, archive, runner=runner)
    archive.write_bytes(b"tampered")
    with pytest.raises(PostgresBackupError, match="verification failed"):
        inspect_postgres_backup(archive)


def test_restore_rejects_a_different_local_database(tmp_path: Path) -> None:
    def runner(command, _env, _timeout) -> None:
        output = next(item.split("=", 1)[1] for item in command if item.startswith("--file="))
        Path(output).write_bytes(b"valid")

    archive = tmp_path / "state.dump"
    create_postgres_backup(URL, archive, runner=runner)
    with pytest.raises(PostgresBackupError, match="does not match"):
        restore_postgres_backup(
            "postgresql://user:secret@localhost:5433/other",
            archive,
        )
