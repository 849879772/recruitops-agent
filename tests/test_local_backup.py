from pathlib import Path
from zipfile import ZipFile

import pytest

from packages.local_backup import create_local_backup, inspect_local_backup, restore_local_backup


def test_backup_excludes_env_and_restores_state_and_profile(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / ".data" / "traces.jsonl").write_text("trace", encoding="utf-8")
    (root / "config" / "candidate_profile.yaml").write_text("name: test", encoding="utf-8")
    (root / ".env").write_text("SECRET=value", encoding="utf-8")
    archive = tmp_path / "backup.zip"

    create_local_backup(root, archive)
    manifest = inspect_local_backup(archive)
    (root / ".data" / "traces.jsonl").write_text("changed", encoding="utf-8")
    restore_local_backup(archive, root, apply=True)

    assert manifest["includes_sqlite_database"] is False
    assert (root / ".data" / "traces.jsonl").read_text(encoding="utf-8") == "trace"
    with ZipFile(archive) as backup:
        assert ".env" not in backup.namelist()
        assert all(b"SECRET=value" not in backup.read(name) for name in backup.namelist())


def test_restore_is_preview_only_without_apply(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".data").mkdir(parents=True)
    source = root / ".data" / "state.txt"
    source.write_text("original", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    create_local_backup(root, archive)
    source.write_text("current", encoding="utf-8")

    restore_local_backup(archive, root)

    assert source.read_text(encoding="utf-8") == "current"


def test_unsafe_archive_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as value:
        value.writestr("../escape", b"bad")
        value.writestr("backup-manifest.json", b'{"format": 1, "files": {}}')
    with pytest.raises(ValueError, match="unsafe"):
        inspect_local_backup(archive)
