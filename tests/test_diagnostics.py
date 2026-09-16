from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from packages.config import Settings
from packages.diagnostics import diagnose_local_installation


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    for name in (
        "extension/manifest.json",
        "extension/popup.html",
        "extension/popup.js",
        "extension/src/background.js",
        "extension/src/content-script.js",
        "scripts/run_local_task.py",
        "scripts/install_windows_tasks.ps1",
        "scripts/uninstall_windows_tasks.ps1",
        "scripts/start_local.ps1",
        "scripts/stop_local.ps1",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (root / "extension/protocol.json").write_text(
        json.dumps({"version": 3}), encoding="utf-8"
    )
    return root


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "data").mkdir(parents=True)
    connection = sqlite3.connect(source / "data/jobs.db")
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    (source / "data/applications.json").write_text("[]", encoding="utf-8")
    (source / "config.yaml").write_text(
        """
profile:
  degree: 硕士
  skills: [C++, Linux]
  matching:
    primary_directions: [C++软件开发]
    project_evidence: [完成机器人软件项目]
""".strip(),
        encoding="utf-8",
    )
    return source


def test_diagnosis_passes_without_exposing_secret_values(tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = _source(tmp_path)
    settings = Settings(
        _env_file=None,
        source_root=source,
        mail_enabled=True,
        mail_imap_host="imap.example.com",
        mail_imap_username="candidate@example.com",
        mail_imap_password="top-secret",
    )

    report = diagnose_local_installation(root, settings)

    assert report["ok"] is True
    assert report["failure_count"] == 0
    assert "top-secret" not in json.dumps(report)
    assert "candidate@example.com" not in json.dumps(report)


def test_diagnosis_reports_unsafe_or_incomplete_configuration(tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = _source(tmp_path)
    (source / "data/jobs.db").unlink()
    settings = Settings(
        _env_file=None,
        source_root=source,
        api_host="0.0.0.0",
        mail_enabled=True,
    )

    report = diagnose_local_installation(root, settings)

    assert report["ok"] is False
    assert report["checks"]["source_database"]["status"] == "fail"
    assert report["checks"]["api_boundary"]["status"] == "fail"
    assert report["checks"]["mail"]["status"] == "fail"
