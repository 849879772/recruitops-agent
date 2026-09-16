from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


MANIFEST_NAME = "backup-manifest.json"
ALLOWED_PREFIXES = ("state/", "profile/")


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        name == MANIFEST_NAME
        or any(name.startswith(prefix) for prefix in ALLOWED_PREFIXES)
        or name == "database/agent.sqlite"
    ) and not path.is_absolute() and ".." not in path.parts


def create_local_backup(
    project_root: Path,
    destination: Path,
    *,
    sqlite_database: Path | None = None,
) -> Path:
    root = project_root.resolve()
    output = destination.resolve()
    if root == output or root in output.parents and output.name == ".env":
        raise ValueError("invalid backup destination")
    files: dict[str, bytes] = {}
    state_root = root / ".data"
    if state_root.exists():
        for source in sorted(path for path in state_root.rglob("*") if path.is_file()):
            if output == source.resolve():
                continue
            files[f"state/{source.relative_to(state_root).as_posix()}"] = source.read_bytes()
    profile = root / "config" / "candidate_profile.yaml"
    if profile.is_file():
        files["profile/candidate_profile.yaml"] = profile.read_bytes()

    with TemporaryDirectory(prefix="recruitops-backup-") as temp_dir:
        if sqlite_database is not None:
            database = sqlite_database.resolve()
            if not database.is_file():
                raise FileNotFoundError(database)
            snapshot = Path(temp_dir) / "agent.sqlite"
            with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            files["database/agent.sqlite"] = snapshot.read_bytes()

        manifest = {
            "format": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "includes_sqlite_database": "database/agent.sqlite" in files,
            "excluded": [".env", "mail credentials", "API keys"],
            "files": {name: _digest(data) for name, data in sorted(files.items())},
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
            )
    return output


def inspect_local_backup(archive_path: Path) -> dict:
    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        if MANIFEST_NAME not in names or any(not _safe_member(name) for name in names):
            raise ValueError("backup contains an unsafe or unsupported path")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("unsupported backup manifest")
        expected_names = set(manifest["files"])
        if expected_names != set(names) - {MANIFEST_NAME}:
            raise ValueError("backup manifest does not match archive contents")
        for name, expected in manifest["files"].items():
            if _digest(archive.read(name)) != expected:
                raise ValueError(f"backup digest mismatch: {name}")
        return manifest


def restore_local_backup(
    archive_path: Path,
    project_root: Path,
    *,
    sqlite_database: Path | None = None,
    apply: bool = False,
) -> dict:
    manifest = inspect_local_backup(archive_path)
    if not apply:
        return manifest
    root = project_root.resolve()
    with ZipFile(archive_path) as archive:
        for name in manifest["files"]:
            if name.startswith("state/"):
                target = root / ".data" / name.removeprefix("state/")
            elif name == "profile/candidate_profile.yaml":
                target = root / "config" / "candidate_profile.yaml"
            elif name == "database/agent.sqlite":
                if sqlite_database is None:
                    raise ValueError("sqlite_database is required to restore the database snapshot")
                target = sqlite_database.resolve()
            else:
                raise ValueError(f"unsupported backup member: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.recruitops-restore")
            temporary.write_bytes(archive.read(name))
            temporary.replace(target)
    return manifest


__all__ = ["create_local_backup", "inspect_local_backup", "restore_local_backup"]
