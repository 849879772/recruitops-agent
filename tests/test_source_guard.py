from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from packages.security.source_guard import (
    SourceChangedError,
    SourceGuardError,
    UnsafeTargetError,
    ensure_target_outside_source_root,
    open_sqlite_read_only,
    snapshot_source,
    source_unchanged,
    verify_source_unchanged,
)
from scripts.verify_legacy_unchanged import main as verify_legacy_main


ROOT = Path(__file__).resolve().parents[1]


def _legacy_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "legacy"
    source_root.mkdir()
    (source_root / "config.yaml").write_text("companies: []\n", encoding="utf-8")
    (source_root / "data").mkdir()
    (source_root / "data" / "applications.json").write_text("[]\n", encoding="utf-8")
    agent_root = source_root / "RecruitOps-Agent"
    (agent_root / ".data").mkdir(parents=True)
    (agent_root / "runtime.txt").write_text("Agent-owned\n", encoding="utf-8")
    manifest_path = agent_root / ".data" / "legacy_source_manifest.json"
    return source_root, agent_root, manifest_path


def test_snapshot_lists_source_files_and_writes_only_agent_manifest(tmp_path: Path) -> None:
    source_root, agent_root, manifest_path = _legacy_tree(tmp_path)

    manifest = snapshot_source(source_root, manifest_path, agent_root=agent_root)

    assert manifest_path.is_file()
    assert manifest["file_count"] == 2
    assert [entry["path"] for entry in manifest["files"]] == [
        "config.yaml",
        "data/applications.json",
    ]
    assert len(manifest["aggregate_sha256"]) == 64
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["aggregate_sha256"] == manifest[
        "aggregate_sha256"
    ]
    assert (agent_root / "runtime.txt").as_posix() not in {
        entry["path"] for entry in manifest["files"]
    }


def test_verify_detects_changed_added_and_removed_source_files(tmp_path: Path) -> None:
    source_root, agent_root, manifest_path = _legacy_tree(tmp_path)
    snapshot_source(source_root, manifest_path, agent_root=agent_root)

    assert verify_source_unchanged(source_root, manifest_path)
    (source_root / "config.yaml").write_text("companies: [changed]\n", encoding="utf-8")
    with pytest.raises(SourceChangedError, match="legacy source changed"):
        verify_source_unchanged(source_root, manifest_path)

    snapshot_source(source_root, manifest_path, agent_root=agent_root)
    (source_root / "new.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(SourceChangedError, match="added=new.txt"):
        verify_source_unchanged(source_root, manifest_path)

    snapshot_source(source_root, manifest_path, agent_root=agent_root)
    (source_root / "data" / "applications.json").unlink()
    with pytest.raises(SourceChangedError, match="removed=data/applications.json"):
        verify_source_unchanged(source_root, manifest_path)


def test_source_unchanged_checks_before_and_after_block(tmp_path: Path) -> None:
    source_root, agent_root, _ = _legacy_tree(tmp_path)

    with source_unchanged(source_root, agent_root=agent_root):
        pass

    with pytest.raises(SourceChangedError):
        with source_unchanged(source_root, agent_root=agent_root):
            (source_root / "config.yaml").write_text("changed\n", encoding="utf-8")


def test_target_guard_rejects_paths_inside_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "legacy"
    source_root.mkdir()
    outside = tmp_path / "agent" / "manifest.json"

    assert ensure_target_outside_source_root(source_root, outside) == outside.resolve()
    with pytest.raises(UnsafeTargetError):
        ensure_target_outside_source_root(source_root, source_root / "output.json")
    with pytest.raises(SourceGuardError):
        ensure_target_outside_source_root(source_root, source_root / "nested" / "../output.json")


def test_sqlite_helper_forces_uri_mode_ro_and_query_only(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
    connection.execute("INSERT INTO jobs (title) VALUES ('read-only')")
    connection.commit()
    connection.close()

    readonly = open_sqlite_read_only(database)
    try:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        assert readonly.execute("SELECT title FROM jobs").fetchone()[0] == "read-only"
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO jobs (title) VALUES ('blocked')")
    finally:
        readonly.close()


def test_cli_supports_snapshot_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source_root, agent_root, manifest_path = _legacy_tree(tmp_path)

    assert (
        verify_legacy_main(
            [
                "snapshot",
                "--source-root",
                str(source_root),
                "--manifest",
                str(manifest_path),
                "--agent-root",
                str(agent_root),
            ]
        )
        == 0
    )
    assert "\"command\": \"snapshot\"" in capsys.readouterr().out

    assert (
        verify_legacy_main(
            [
                "verify",
                "--source-root",
                str(source_root),
                "--manifest",
                str(manifest_path),
                "--agent-root",
                str(agent_root),
            ]
        )
        == 0
    )
    assert "\"unchanged\": true" in capsys.readouterr().out

    (source_root / "config.yaml").write_text("changed\n", encoding="utf-8")
    assert (
        verify_legacy_main(
            [
                "verify",
                "--source-root",
                str(source_root),
                "--manifest",
                str(manifest_path),
                "--agent-root",
                str(agent_root),
            ]
        )
        == 1
    )
    assert "\"unchanged\": false" in capsys.readouterr().out


def test_compose_does_not_mount_legacy_source() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    source_mounts = [
        volume
        for volume in compose["services"]["api"]["volumes"]
        if isinstance(volume, dict) and volume.get("target") == "/source"
    ]

    assert source_mounts == []


def test_env_example_declares_agent_owned_manifest_path() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RECRUITOPS_SOURCE_MANIFEST=.data/legacy_source_manifest.json" in env_example
