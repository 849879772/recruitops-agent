"""Read-only bundle verification and explicit writable instance boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import RuntimeFailure
from .instance import reject_links


REQUIRED = {
    "python", "codex", "node", "chromium", "postgres", "initdb", "psql",
    "pg_dump", "pg_restore", "pg_ctl", "pg_config", "vector_dll", "vector_control", "vector_sql",
    "api_bootstrap", "migration_script",
}


def inside(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PureWindowsPath(relative).drive or ":" in relative):
        raise RuntimeFailure("unsafe_resource_path")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
           for part in relative.split("/")):
        raise RuntimeFailure("unsafe_resource_path")
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root) or ".." in Path(relative).parts:
        raise RuntimeFailure("unsafe_resource_path")
    return path


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict

    def resource(self, name: str) -> Path:
        return inside(self.root, self.manifest["entrypoints"][name])

    def verify(self) -> None:
        try:
            reject_links(self.root)
        except RuntimeFailure as exc:
            raise RuntimeFailure("linked_resource") from exc
        m = self.manifest
        if m.get("schema") != 1 or m.get("platform") != "windows-x64":
            raise RuntimeFailure("unsupported_manifest")
        if m.get("postgres_major") != 16:
            raise RuntimeFailure("postgres_major_mismatch")
        files, entries = m.get("files", {}), m.get("entrypoints", {})
        if not isinstance(files, dict) or not isinstance(entries, dict) or not REQUIRED <= entries.keys():
            raise RuntimeFailure("incomplete_manifest")
        for name in REQUIRED:
            if not isinstance(entries[name], str) or entries[name] not in files:
                raise RuntimeFailure("untracked_entrypoint")
        components = m.get("components", {})
        if not isinstance(components, dict):
            raise RuntimeFailure("incomplete_manifest")
        for name in ("python", "codex", "node", "chromium", "postgres", "pgvector", "application"):
            component = components.get(name, {})
            if not isinstance(component, dict):
                raise RuntimeFailure("incomplete_manifest")
            version = component.get("version", "")
            if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?", version):
                raise RuntimeFailure("unpinned_component")
            source, license_file = component.get("source", ""), component.get("license_file")
            if (not isinstance(source, str) or not source.startswith("https://")
                    or not isinstance(license_file, str) or license_file not in files):
                raise RuntimeFailure("missing_license_or_source")
        if m["components"]["postgres"]["version"].split(".")[0] != "16":
            raise RuntimeFailure("postgres_major_mismatch")
        for relative, digest in files.items():
            path = inside(self.root, relative)
            if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                raise RuntimeFailure("invalid_hash")
            if not path.is_file():
                raise RuntimeFailure("missing_resource")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise RuntimeFailure("hash_mismatch")
            if path.suffix.lower() in {".exe", ".dll", ".pyd"}:
                verify_pe_x64(path)
        # All staged files must be accounted for, including DLLs and Python modules.
        for path in self.root.rglob("*"):
            if path.is_symlink() or (os.name == "nt" and path.lstat().st_file_attributes & 0x400):
                raise RuntimeFailure("linked_resource")
            if path.is_file() and path != self.root / "runtime-manifest.json" and path.relative_to(self.root).as_posix() not in files:
                raise RuntimeFailure("untracked_resource")
        pgroot = self.resource("postgres").parent.parent
        expected = {
            "vector_dll": pgroot / "lib/vector.dll",
            "vector_control": pgroot / "share/extension/vector.control",
        }
        for name, path in expected.items():
            if self.resource(name) != path:
                raise RuntimeFailure("invalid_pg_layout")
        if self.resource("vector_sql").parent != pgroot / "share/extension":
            raise RuntimeFailure("invalid_pg_layout")
        for name in ("initdb", "psql", "pg_dump", "pg_restore", "pg_ctl", "pg_config"):
            if self.resource(name).parent != pgroot / "bin":
                raise RuntimeFailure("invalid_pg_layout")
        if not any(p.startswith("app/migrations/") and p.endswith(".sql") for p in files):
            raise RuntimeFailure("missing_migrations")
        vector_version = m["components"]["pgvector"]["version"]
        if self.resource("vector_sql").name != f"vector--{vector_version}.sql":
            raise RuntimeFailure("pgvector_version_mismatch")
        control = self.resource("vector_control").read_text(encoding="utf-8")
        if not re.search(r"^\s*default_version\s*=\s*'" + re.escape(vector_version) + r"'\s*(?:#.*)?$", control, re.M):
            raise RuntimeFailure("pgvector_version_mismatch")

    @classmethod
    def load(cls, root: Path) -> "Bundle":
        root = root.resolve()
        try:
            manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeFailure("manifest_unavailable") from exc
        if not isinstance(manifest, dict):
            raise RuntimeFailure("unsupported_manifest")
        bundle = cls(root, manifest)
        bundle.verify()
        return bundle


def verify_pe_x64(path: Path) -> None:
    """Reject wrong-architecture/native placeholders, not a loader dependency audit."""
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise RuntimeFailure("invalid_pe_binary")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        signature = stream.read(6)
    if len(signature) != 6 or signature[:4] != b"PE\0\0" or struct.unpack_from("<H", signature, 4)[0] != 0x8664:
        raise RuntimeFailure("binary_architecture_mismatch")


@dataclass(frozen=True)
class Layout:
    resources: Path
    data: Path

    def validate(self, repository: Path) -> None:
        resources, data, repo = self.resources.resolve(), self.data.resolve(), repository.resolve()
        isolated = repo / ".desktop-runtime-tests"
        if data == isolated or not data.is_relative_to(isolated):
            raise RuntimeFailure("isolated_instance_required")
        if resources == data or resources.is_relative_to(data) or data.is_relative_to(resources):
            raise RuntimeFailure("resource_data_overlap")
        # Do not follow an existing junction anywhere under the test root.
        for path in (isolated, *self.data.absolute().parents, self.data.absolute()):
            if path.is_symlink() or (path.exists() and os.name == "nt" and path.lstat().st_file_attributes & 0x400):
                raise RuntimeFailure("linked_instance")

    def prepare(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        for name in ("home", "tmp", "config", "logs", "backups", "browser", "codex", "source"):
            (self.data / name).mkdir(exist_ok=True)
