"""Nondestructive PostgreSQL backup and restore-preview validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.postgres_backup import (
    PostgresBackupError,
    create_postgres_backup,
    inspect_postgres_backup,
    restore_postgres_backup,
)


class PostgresDrillReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    archive: str
    backup_verified: bool = False
    restore_preview_verified: bool = False
    restore_apply_called: bool = False
    destructive_operations: int = Field(default=0, ge=0)
    runner_calls: int = Field(default=0, ge=0)
    password_in_command: bool = False
    target: dict[str, Any] = Field(default_factory=dict)
    failure_category: str | None = None
    detail: str = ""


CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], None]


def _recording_runner(
    delegate: CommandRunner,
    calls: list[tuple[list[str], dict[str, str]]],
) -> CommandRunner:
    def run(command: Sequence[str], env: Mapping[str, str], timeout: float) -> None:
        calls.append((list(command), dict(env)))
        delegate(command, env, timeout)

    return run


def run_postgres_backup_drill(
    database_url: str,
    archive: Path,
    *,
    runner: CommandRunner | None = None,
) -> PostgresDrillReport:
    """Create, verify, and preview-restore a local backup without pg_restore.

    The optional runner is intended for tests or a controlled command wrapper.
    This function deliberately has no ``apply`` argument: a restore drill is
    always preview-only and therefore cannot alter the database.
    """

    calls: list[tuple[list[str], dict[str, str]]] = []
    kwargs: dict[str, Any] = {}
    if runner is not None:
        kwargs["runner"] = _recording_runner(runner, calls)
    try:
        manifest = create_postgres_backup(database_url, archive, **kwargs)
        inspected = inspect_postgres_backup(archive)
        preview = restore_postgres_backup(database_url, archive, apply=False, **kwargs)
        backup_verified = inspected == manifest
        preview_verified = (
            preview.get("restore_status") == "preview"
            and preview.get("sha256") == manifest.get("sha256")
            and preview.get("target") == manifest.get("target")
        )
        password_in_command = any(
            "PGPASSWORD" in env and any(env["PGPASSWORD"] in item for item in command)
            for command, env in calls
            if env.get("PGPASSWORD")
        )
        return PostgresDrillReport(
            status="passed" if backup_verified and preview_verified else "failed",
            archive=str(Path(archive).expanduser().resolve()),
            backup_verified=backup_verified,
            restore_preview_verified=preview_verified,
            restore_apply_called=False,
            destructive_operations=0,
            runner_calls=len(calls) if runner is not None else 1,
            password_in_command=password_in_command,
            target=dict(manifest.get("target") or {}),
            failure_category=(
                None if backup_verified and preview_verified else "verification_failed"
            ),
        )
    except (PostgresBackupError, OSError, ValueError) as exc:
        return PostgresDrillReport(
            status="failed",
            archive=str(Path(archive).expanduser().resolve()),
            runner_calls=len(calls),
            restore_apply_called=False,
            destructive_operations=0,
            password_in_command=False,
            failure_category="backup_drill_error",
            detail=str(exc),
        )


def render_postgres_drill_report(report: PostgresDrillReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "PostgresDrillReport",
    "render_postgres_drill_report",
    "run_postgres_backup_drill",
]
