"""Read-only guardrails for the legacy autumn-system source.

The Agent is allowed to read the legacy source, but its state and manifests
must remain outside that source.  This module deliberately uses only the
standard library so the guard is also usable by maintenance scripts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterator


MANIFEST_FORMAT = "recruitops-legacy-source"
MANIFEST_VERSION = 1
AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = AGENT_ROOT / ".data" / "legacy_source_manifest.json"
DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024


class SourceGuardError(RuntimeError):
    """Base error for a failed legacy-source guard check."""


class UnsafeTargetError(SourceGuardError):
    """Raised when an Agent-owned target resolves inside the legacy source."""


class ManifestError(SourceGuardError):
    """Raised when a source manifest is missing or malformed."""


class SourceChangedError(SourceGuardError):
    """Raised when the current source differs from the saved manifest."""


PathLike = str | os.PathLike[str]


def _resolve_path(value: PathLike, *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SourceGuardError(f"invalid {label} path: {value!r}") from exc


def _source_root(value: PathLike) -> Path:
    root = _resolve_path(value, label="source root")
    if not root.is_dir():
        raise SourceGuardError(f"source root is not a directory: {root}")
    return root


def _is_within(path: Path, root: Path) -> bool:
    """Return true for ``root`` itself and descendants, including Windows case."""

    try:
        common = os.path.commonpath((str(path), str(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def ensure_target_outside_source_root(
    source_root: PathLike,
    target: PathLike,
    *,
    allowed_root: PathLike | None = None,
) -> Path:
    """Resolve ``target`` and reject it when it is inside ``source_root``.

    ``allowed_root`` exists only for the Agent's own state directory.  It is
    useful when the Agent checkout is nested below the legacy checkout, as it
    is in the local layout; callers must opt into that exception explicitly.
    """

    root = _source_root(source_root)
    resolved_target = _resolve_path(target, label="target")
    if allowed_root is not None:
        resolved_allowed_root = _resolve_path(allowed_root, label="allowed root")
        if _is_within(resolved_target, resolved_allowed_root):
            return resolved_target
    if _is_within(resolved_target, root):
        raise UnsafeTargetError(
            f"target path must be outside source root: {resolved_target} is under {root}"
        )
    return resolved_target


assert_target_outside_source_root = ensure_target_outside_source_root


def _normalise_excluded_roots(
    root: Path,
    excluded_roots: Iterable[PathLike] = (),
    *,
    agent_root: PathLike | None = AGENT_ROOT,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if agent_root is not None:
        candidates.append(_resolve_path(agent_root, label="Agent root"))
    candidates.extend(_resolve_path(path, label="excluded root") for path in excluded_roots)

    result: list[Path] = []
    for candidate in candidates:
        if not _is_within(candidate, root) or candidate == root:
            continue
        if candidate not in result:
            result.append(candidate)
    return tuple(sorted(result, key=lambda path: path.as_posix()))


def _iter_source_files(
    root: Path,
    excluded_roots: tuple[Path, ...],
    *,
    ignored_directory_names: Iterable[str],
) -> Iterator[Path]:
    ignored = frozenset(ignored_directory_names)
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in ignored
            and not any(
                _is_within((current_path / name).resolve(strict=False), excluded)
                for excluded in excluded_roots
            )
        )
        for name in sorted(filenames):
            candidate = current_path / name
            resolved = candidate.resolve(strict=False)
            if any(_is_within(resolved, excluded) for excluded in excluded_roots):
                continue
            if candidate.is_symlink() and not _is_within(resolved, root):
                raise SourceGuardError(
                    f"source symlink escapes source root: {candidate} -> {resolved}"
                )
            if not candidate.is_file():
                continue
            yield candidate


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        after = path.stat()
    except (OSError, ValueError) as exc:
        raise SourceGuardError(f"unable to hash source file: {path}") from exc

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
    ):
        raise SourceChangedError(f"source file changed while hashing: {path}")
    return digest.hexdigest(), after.st_size


def _aggregate_sha256(files: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in files:
        path = str(entry["path"])
        sha256 = str(entry["sha256"])
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_source_manifest(
    source_root: PathLike,
    *,
    excluded_roots: Iterable[PathLike] = (),
    agent_root: PathLike | None = AGENT_ROOT,
    ignored_directory_names: Iterable[str] = DEFAULT_IGNORED_DIRECTORY_NAMES,
) -> dict[str, Any]:
    """Build a deterministic manifest for all regular source files."""

    root = _source_root(source_root)
    excluded = _normalise_excluded_roots(
        root,
        excluded_roots,
        agent_root=agent_root,
    )
    files: list[dict[str, Any]] = []
    for path in _iter_source_files(
        root,
        excluded,
        ignored_directory_names=ignored_directory_names,
    ):
        digest, size = _hash_file(path)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": size,
                "sha256": digest,
            }
        )
    files.sort(key=lambda entry: str(entry["path"]))
    relative_excluded = [path.relative_to(root).as_posix() for path in excluded]
    return {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "source_root": str(root),
        "excluded_roots": relative_excluded,
        "files": files,
        "file_count": len(files),
        "aggregate_sha256": _aggregate_sha256(files),
    }


create_source_manifest = build_source_manifest


def _manifest_target(path: PathLike | None) -> Path:
    if path is None:
        return DEFAULT_MANIFEST_PATH
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target.resolve(strict=False)


def _write_manifest(manifest: Mapping[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise SourceGuardError(f"unable to write source manifest: {target}") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def snapshot_source(
    source_root: PathLike,
    manifest_path: PathLike | None = None,
    *,
    excluded_roots: Iterable[PathLike] = (),
    agent_root: PathLike | None = AGENT_ROOT,
    ignored_directory_names: Iterable[str] = DEFAULT_IGNORED_DIRECTORY_NAMES,
) -> dict[str, Any]:
    """Build and persist a source manifest without writing to the source."""

    root = _source_root(source_root)
    target = _manifest_target(manifest_path)
    allowed_root = agent_root if agent_root is not None else None
    ensure_target_outside_source_root(root, target, allowed_root=allowed_root)
    manifest = build_source_manifest(
        root,
        excluded_roots=excluded_roots,
        agent_root=agent_root,
        ignored_directory_names=ignored_directory_names,
    )
    _write_manifest(manifest, target)
    return manifest


snapshot_source_files = snapshot_source


def load_source_manifest(path: PathLike) -> dict[str, Any]:
    """Load and validate a JSON source manifest."""

    manifest_path = _resolve_path(path, label="manifest")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"unable to read source manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("source manifest must contain a JSON object")
    if payload.get("format") != MANIFEST_FORMAT or payload.get("version") != MANIFEST_VERSION:
        raise ManifestError("unsupported source manifest format or version")
    if not isinstance(payload.get("source_root"), str) or not payload["source_root"].strip():
        raise ManifestError("source manifest has no source_root")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ManifestError("source manifest files must be a list")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ManifestError("source manifest file entries must be objects")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ManifestError("source manifest contains an invalid relative path")
        if Path(relative).as_posix() != relative or ".." in Path(relative).parts:
            raise ManifestError("source manifest contains a path outside the source root")
        if relative in seen or not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ManifestError("source manifest contains duplicate or invalid file hashes")
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ManifestError("source manifest contains an invalid file size")
        seen.add(relative)
    aggregate = payload.get("aggregate_sha256")
    if not isinstance(aggregate, str) or not _SHA256_RE.fullmatch(aggregate):
        raise ManifestError("source manifest has an invalid aggregate_sha256")
    if payload.get("file_count") != len(files):
        raise ManifestError("source manifest file_count does not match files")
    if _aggregate_sha256(sorted(files, key=lambda entry: str(entry["path"]))) != aggregate:
        raise ManifestError("source manifest aggregate_sha256 does not match files")
    excluded = payload.get("excluded_roots", [])
    if not isinstance(excluded, list) or not all(
        isinstance(item, str)
        and item
        and not Path(item).is_absolute()
        and Path(item).as_posix() == item
        and ".." not in Path(item).parts
        for item in excluded
    ):
        raise ManifestError("source manifest excluded_roots must be relative paths")
    return payload


load_manifest = load_source_manifest


def _expected_manifest(
    expected: Mapping[str, Any] | PathLike | None,
    *,
    manifest_path: PathLike | None,
) -> dict[str, Any]:
    if expected is None:
        if manifest_path is None:
            manifest_path = DEFAULT_MANIFEST_PATH
        return load_source_manifest(manifest_path)
    if isinstance(expected, Mapping):
        candidate = dict(expected)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="source-manifest-",
            suffix=".json",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(
                json.dumps(candidate, ensure_ascii=False),
                encoding="utf-8",
            )
            return load_source_manifest(temporary)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    return load_source_manifest(expected)


def _manifest_file_map(manifest: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    return {
        str(entry["path"]): (int(entry["size"]), str(entry["sha256"]))
        for entry in manifest["files"]
    }


def compare_source_manifests(
    expected: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Raise when two manifests differ; return true for an exact match."""

    expected_files = _manifest_file_map(expected)
    current_files = _manifest_file_map(current)
    if (
        expected.get("aggregate_sha256") == current.get("aggregate_sha256")
        and expected_files == current_files
    ):
        return True

    added = sorted(set(current_files) - set(expected_files))
    removed = sorted(set(expected_files) - set(current_files))
    changed = sorted(
        path
        for path in set(expected_files) & set(current_files)
        if expected_files[path] != current_files[path]
    )
    details: list[str] = []
    if added:
        details.append(f"added={','.join(added[:5])}")
    if removed:
        details.append(f"removed={','.join(removed[:5])}")
    if changed:
        details.append(f"changed={','.join(changed[:5])}")
    summary = "; ".join(details) or "aggregate hash changed"
    raise SourceChangedError(f"legacy source changed: {summary}")


def verify_source_unchanged(
    source_root: PathLike,
    expected: Mapping[str, Any] | PathLike | None = None,
    *,
    manifest_path: PathLike | None = None,
    agent_root: PathLike | None = AGENT_ROOT,
    ignored_directory_names: Iterable[str] = DEFAULT_IGNORED_DIRECTORY_NAMES,
) -> bool:
    """Verify the current source against a saved manifest."""

    root = _source_root(source_root)
    manifest = _expected_manifest(expected, manifest_path=manifest_path)
    saved_root = _resolve_path(str(manifest["source_root"]), label="manifest source root")
    if saved_root != root:
        raise ManifestError(
            f"manifest source root does not match: {saved_root} != {root}"
        )
    excluded_roots = [root / str(relative) for relative in manifest.get("excluded_roots", [])]
    current = build_source_manifest(
        root,
        excluded_roots=excluded_roots,
        agent_root=None,
        ignored_directory_names=ignored_directory_names,
    )
    return compare_source_manifests(manifest, current)


verify_snapshot = verify_source_unchanged
assert_source_unchanged = verify_source_unchanged


@contextmanager
def source_unchanged(
    source_root: PathLike,
    *,
    agent_root: PathLike | None = AGENT_ROOT,
    excluded_roots: Iterable[PathLike] = (),
    ignored_directory_names: Iterable[str] = DEFAULT_IGNORED_DIRECTORY_NAMES,
) -> Iterator[dict[str, Any]]:
    """Capture a before-state and fail after the block if the source changed."""

    root = _source_root(source_root)
    before = build_source_manifest(
        root,
        excluded_roots=excluded_roots,
        agent_root=agent_root,
        ignored_directory_names=ignored_directory_names,
    )
    yield before
    after = build_source_manifest(
        root,
        excluded_roots=excluded_roots,
        agent_root=agent_root,
        ignored_directory_names=ignored_directory_names,
    )
    compare_source_manifests(before, after)


def sqlite_read_only_uri(database: PathLike) -> str:
    """Return a SQLite URI that can only open an existing database read-only."""

    path = _resolve_path(database, label="SQLite database")
    if not path.is_file():
        raise SourceGuardError(f"SQLite database does not exist: {path}")
    return f"{path.as_uri()}?mode=ro"


def open_sqlite_read_only(
    database: PathLike,
    *,
    timeout: float = 5.0,
    row_factory: Any = sqlite3.Row,
) -> sqlite3.Connection:
    """Open an existing SQLite database with URI ``mode=ro`` enforced."""

    uri = sqlite_read_only_uri(database)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        connection.row_factory = row_factory
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
        if query_only != 1:
            raise sqlite3.OperationalError("SQLite query_only pragma was not enabled")
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise SourceGuardError(f"unable to open SQLite database read-only: {uri}") from exc
    return connection


open_read_only_sqlite = open_sqlite_read_only
connect_sqlite_read_only = open_sqlite_read_only


__all__ = [
    "AGENT_ROOT",
    "DEFAULT_IGNORED_DIRECTORY_NAMES",
    "DEFAULT_MANIFEST_PATH",
    "MANIFEST_FORMAT",
    "MANIFEST_VERSION",
    "ManifestError",
    "SourceChangedError",
    "SourceGuardError",
    "UnsafeTargetError",
    "assert_source_unchanged",
    "assert_target_outside_source_root",
    "build_source_manifest",
    "compare_source_manifests",
    "connect_sqlite_read_only",
    "create_source_manifest",
    "ensure_target_outside_source_root",
    "load_manifest",
    "load_source_manifest",
    "open_read_only_sqlite",
    "open_sqlite_read_only",
    "snapshot_source",
    "snapshot_source_files",
    "source_unchanged",
    "sqlite_read_only_uri",
    "verify_snapshot",
    "verify_source_unchanged",
]
