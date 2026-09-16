from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from packages.candidate_profile import CandidateProfileError, load_candidate_profile
from packages.config import Settings


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_EXTENSION_FILES = (
    "manifest.json",
    "protocol.json",
    "popup.html",
    "popup.js",
    "src/background.js",
    "src/content-script.js",
)
_SCHEDULER_FILES = (
    "scripts/run_local_task.py",
    "scripts/install_windows_tasks.ps1",
    "scripts/uninstall_windows_tasks.ps1",
    "scripts/start_local.ps1",
    "scripts/stop_local.ps1",
)
_EXTENSION_PROTOCOL_VERSION = 3


def _result(status: str, detail: str) -> dict[str, str | bool]:
    return {"ok": status != "fail", "status": status, "detail": detail}


def _sqlite_check(path: Path) -> dict[str, str | bool]:
    if not path.is_file():
        return _result("fail", "source database is missing")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return _result("fail", "source database is not readable in read-only mode")
    return _result("pass", "source database opened read-only")


def _profile_check(path: Path) -> dict[str, str | bool]:
    try:
        profile = load_candidate_profile(path)
    except CandidateProfileError:
        return _result("fail", "source profile is missing or invalid")
    evidence_count = len(profile.skills) + len(profile.matching.project_evidence)
    if not profile.matching.primary_directions or evidence_count == 0:
        return _result("fail", "source profile lacks directions or evidence")
    return _result("pass", "structured candidate profile is valid")


def _mail_check(settings: Settings) -> dict[str, str | bool]:
    if not settings.mail_enabled:
        return _result("pass", "mail sync is disabled")
    required = (
        settings.mail_imap_host,
        settings.mail_imap_username,
        settings.mail_imap_password,
        settings.mail_imap_mailbox,
    )
    if not all(value.strip() for value in required):
        return _result("fail", "mail sync is enabled but configuration is incomplete")
    if not 1 <= settings.mail_imap_port <= 65_535:
        return _result("fail", "mail IMAP port is invalid")
    return _result("pass", "mail sync configuration is complete")


def _file_set_check(root: Path, names: tuple[str, ...], detail: str) -> dict[str, Any]:
    missing = [name for name in names if not (root / name).is_file()]
    if missing:
        return {
            **_result("fail", f"{detail} files are missing"),
            "missing": missing,
        }
    return _result("pass", f"{detail} files are present")


def _extension_check(root: Path) -> dict[str, Any]:
    check = _file_set_check(root / "extension", _EXTENSION_FILES, "browser extension")
    if not check["ok"]:
        return check
    try:
        protocol = json.loads((root / "extension" / "protocol.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _result("fail", "browser extension protocol is invalid")
    if protocol.get("version") != _EXTENSION_PROTOCOL_VERSION:
        return _result(
            "fail",
            f"browser extension protocol version is not {_EXTENSION_PROTOCOL_VERSION}",
        )
    return _result(
        "pass",
        f"browser extension protocol v{_EXTENSION_PROTOCOL_VERSION} is valid",
    )


def _writable_parent(path: Path) -> dict[str, str | bool]:
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK):
        return _result("fail", "local state parent is not writable")
    return _result("pass", "local state parent is writable")


def diagnose_local_installation(root: Path, settings: Settings) -> dict[str, Any]:
    """Return a secret-free, non-networked diagnosis of the local installation."""

    resolved_root = root.expanduser().resolve()
    checks: dict[str, Any] = {
        "source_database": _sqlite_check(settings.source_database),
        "source_applications": (
            _result("pass", "source applications file is present")
            if settings.source_applications.is_file()
            else _result("fail", "source applications file is missing")
        ),
        "candidate_profile": _profile_check(settings.source_config),
        "mail": _mail_check(settings),
        "extension": _extension_check(resolved_root),
        "scheduler": _file_set_check(resolved_root, _SCHEDULER_FILES, "scheduler"),
        "local_state": _writable_parent(resolved_root / ".data"),
        "api_boundary": (
            _result("pass", "API host is loopback-only")
            if settings.api_host.casefold() in _LOOPBACK_HOSTS
            else _result("fail", "API host is not loopback-only")
        ),
        "write_mode": (
            _result("warning", "source writes are enabled; approvals and backups are required")
            if settings.write_enabled
            else _result("pass", "source writes are disabled")
        ),
        "postgres_backup_utilities": (
            _result("pass", "pg_dump and pg_restore are available")
            if shutil.which("pg_dump") and shutil.which("pg_restore")
            else _result("warning", "pg_dump or pg_restore is not on PATH")
        ),
    }
    failures = [name for name, check in checks.items() if check["status"] == "fail"]
    warnings = [name for name, check in checks.items() if check["status"] == "warning"]
    return {
        "ok": not failures,
        "checks": checks,
        "failure_count": len(failures),
        "warning_count": len(warnings),
    }


__all__ = ["diagnose_local_installation"]
