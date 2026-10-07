"""Plan or atomically promote business sources into a stopped desktop installation.

No packaging or data copies are performed. A failed promotion restores only the
changed files; --rollback can also recover a prepared backup after interruption.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import Bundle, inside  # noqa: E402
from packages.desktop_runtime.instance import extended_path  # noqa: E402
from packages.desktop_runtime.staging import digest  # noqa: E402

RUNTIME = "resources/desktop-runtime"
MANIFEST = RUNTIME + "/runtime-manifest.json"
PROVENANCE = RUNTIME + "/provenance/application-files.json"
REPORT = "promotion-report.json"


def plain_path(path: Path) -> Path:
    """Check lexical ancestors before resolve() can hide a junction or symlink."""
    path = path.absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"linked path rejected: {part}")
    return path.resolve()


def child(root: Path, relative: str) -> Path:
    # inside() rejects Windows drive/ADS/reserved names and traversal on all OSes.
    inside(root, relative)
    path = plain_path(root / relative)
    if path == root or not path.is_relative_to(root):
        raise ValueError("path escapes validated directory")
    return path


def formal_path(repository: Path, formal: Path | None = None) -> Path:
    expected = plain_path(repository / "artifacts/releases/RecruitOps")
    if formal is not None and plain_path(formal) != expected:
        raise ValueError("only repository/artifacts/releases/RecruitOps is permitted")
    if not expected.is_dir():
        raise ValueError("formal desktop installation missing")
    return expected


def backup_path(repository: Path, formal: Path, backup: Path) -> Path:
    backup = plain_path(backup)
    releases = plain_path(repository / "artifacts/releases")
    if (backup == releases or not backup.is_relative_to(releases)
            or backup == formal or backup.is_relative_to(formal)
            or formal.is_relative_to(backup)):
        raise ValueError("backup must be a separate child of artifacts/releases")
    return backup


def data_inventory(formal: Path) -> dict:
    """Read metadata only: databases and browser profiles are never copied/opened."""
    root = child(formal, ".data")
    if not root.exists():
        raise ValueError("formal .data directory missing")
    rows, count, size = [], 0, 0
    tree_root = extended_path(root)
    pending = [tree_root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"linked path rejected: {path}")
        relative = path.relative_to(tree_root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            rows.append([relative, "directory"])
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            count += 1
            size += info.st_size
            rows.append([relative, info.st_size, info.st_mtime_ns])
        else:
            raise ValueError("unexpected user data entry")
    return {"files": count, "bytes": size, "metadata_sha256": hashlib.sha256(
        json.dumps(sorted(rows), separators=(",", ":")).encode()).hexdigest()}


def _scan_processes(formal: Path) -> list[dict]:
    if os.name != "nt":
        raise RuntimeError("Windows process guard required")
    # Pass the path via stdin, not CommandLine, so the scanner never matches its
    # own path literal. Exclude just the scanner and this tool, not all shells.
    script = r"""
$ErrorActionPreference = 'Stop'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$root = $request.root.Replace('/', '\').TrimEnd('\')
$rows = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.ProcessId -ne $request.tool_pid -and (
      ($_.ExecutablePath -and $_.ExecutablePath.Replace('/', '\').StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) -or
      ($_.CommandLine -and $_.CommandLine.Replace('/', '\').IndexOf($root + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0)
    )
} | Select-Object ProcessId, ExecutablePath)
ConvertTo-Json -InputObject $rows -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps({"root": str(formal), "tool_pid": os.getpid()}),
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("invalid process guard result")
    return rows


def _process_exit_code(pid: int) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        kernel.CloseHandle(handle)


def running_processes(formal: Path) -> list[dict]:
    # CIM can retain an exited row while a handle is held. Ignore only a native
    # confirmed exit; query errors fail closed. Never signal or kill a process.
    return [row for row in _scan_processes(formal)
            if _process_exit_code(int(row["ProcessId"])) == 259]


def assert_stopped(formal: Path) -> None:
    running = running_processes(formal)
    if running:
        raise RuntimeError(f"formal desktop is still running ({len(running)} processes); exit normally first")


def source_inventory(repository: Path, runtime: Path) -> list[str]:
    script = "process.stdout.write(JSON.stringify(require('./apps/desktop/packaging/resources.cjs').sourceInventory(process.cwd())))"
    files = json.loads(subprocess.check_output(
        [str(child(runtime, "node/node.exe")), "-e", script], cwd=repository, text=True,
        encoding="utf-8", timeout=30))
    if not isinstance(files, list) or len(files) != len(set(p.casefold() for p in files)):
        raise ValueError("invalid sourceInventory")
    for relative in files:
        if not child(repository, relative).is_file():
            raise ValueError(f"sourceInventory file missing: {relative}")
    return sorted(files)


def business_source(relative: str) -> bool:
    if relative.startswith("apps/api/"):
        return relative.endswith(".py")
    if relative.startswith("apps/web/"):
        return Path(relative).suffix.lower() in {
            ".js", ".mjs", ".cjs", ".css", ".html", ".json", ".svg", ".png", ".ico", ".woff", ".woff2"}
    if relative.startswith("packages/") and relative.endswith(".py"):
        return relative.split("/")[1] not in {"desktop_runtime", "desktop_browser", "desktop_filler"}
    return False


def json_bytes(value) -> bytes:
    return json.dumps(value, indent=2).encode("utf-8")


def make_plan(repository: Path, formal: Path | None = None) -> dict:
    repository = plain_path(repository)
    formal = formal_path(repository, formal)
    runtime = child(formal, RUNTIME)
    bundle = Bundle.load(runtime)
    manifest = bundle.manifest
    manifest_path = child(formal, MANIFEST)
    provenance_path = child(formal, PROVENANCE)
    previous = json.loads(provenance_path.read_text(encoding="utf-8"))
    tracked = {p[4:] for p in manifest["files"] if p.startswith("app/")}
    if (not isinstance(previous, list) or len(previous) != len(set(previous))
            or set(previous) != tracked):
        raise ValueError("application provenance does not match runtime manifest")
    sources = source_inventory(repository, runtime)
    removed = sorted(tracked - set(sources))
    if removed:
        raise ValueError(f"removed sources require full desktop packaging: {removed}")
    changes = []
    updated = json.loads(json.dumps(manifest))
    for relative in sources:
        source = child(repository, relative)
        new_hash = digest(source)
        old_hash = manifest["files"].get("app/" + relative)
        if new_hash == old_hash:
            continue
        if not business_source(relative):
            raise ValueError(f"non-business source change requires full desktop packaging: {relative}")
        target = RUNTIME + "/app/" + relative
        child(formal, target)
        changes.append({"path": target, "source": relative, "old_sha256": old_hash,
                        "new_sha256": new_hash, "bytes": source.stat().st_size})
        updated["files"]["app/" + relative] = new_hash
    if changes:
        provenance = json_bytes(sources)
        provenance_hash = hashlib.sha256(provenance).hexdigest()
        updated["files"]["provenance/application-files.json"] = provenance_hash
        changes.extend([
            {"path": PROVENANCE, "old_sha256": digest(provenance_path),
             "new_sha256": provenance_hash, "bytes": len(provenance)},
            {"path": MANIFEST, "old_sha256": digest(manifest_path),
             "new_sha256": hashlib.sha256(json_bytes(updated)).hexdigest(), "bytes": len(json_bytes(updated))},
        ])
    return {"schema": 1, "repository": str(repository), "formal": str(formal),
            "changes": changes, "manifest": updated, "sources": sources,
            "data": data_inventory(formal), "running_processes": running_processes(formal),
            "backup_bytes": sum(child(formal, c["path"]).stat().st_size
                                for c in changes if c["old_sha256"] is not None)}


def _atomic_bytes(target: Path, content: bytes, expected: str) -> None:
    plain_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    plain_path(target.parent)
    fd, name = tempfile.mkstemp(prefix=".source-promotion-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if digest(temporary) != expected:
            raise ValueError("replacement hash mismatch")
        plain_path(target)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_report(backup: Path, report: dict) -> None:
    content = json_bytes(report)
    _atomic_bytes(child(backup, REPORT), content, hashlib.sha256(content).hexdigest())


def _check_current(formal: Path, change: dict, expected: str | None) -> None:
    target = child(formal, change["path"])
    if expected is None:
        if target.exists():
            raise ValueError(f"new target already exists: {change['path']}")
    elif not target.is_file() or digest(target) != expected:
        raise ValueError(f"current file hash mismatch: {change['path']}")


def rollback_backup(repository: Path, backup: Path) -> dict:
    repository = plain_path(repository)
    formal = formal_path(repository)
    backup = backup_path(repository, formal, backup)
    report = json.loads(child(backup, REPORT).read_text(encoding="utf-8"))
    if (report.get("schema") != 1 or report.get("repository") != str(repository)
            or report.get("formal") != str(formal)):
        raise ValueError("backup belongs to a different installation")
    changes = report["changes"]
    if not isinstance(changes, list) or len(changes) != len({c["path"].casefold() for c in changes}):
        raise ValueError("invalid rollback inventory")
    # Validate every backup and current target before modifying anything. An
    # unrelated post-promotion edit must never be silently overwritten.
    for change in changes:
        relative = change["path"]
        if relative not in {MANIFEST, PROVENANCE}:
            if not relative.startswith(RUNTIME + "/app/") or not business_source(relative[len(RUNTIME + "/app/"):]):
                raise ValueError("invalid rollback source path")
        target = child(formal, relative)
        if target.exists() and not target.is_file():
            raise ValueError(f"rollback target is not a file: {relative}")
        current = digest(target) if target.is_file() else None
        if current not in {change["old_sha256"], change["new_sha256"]}:
            raise ValueError(f"rollback target has changed: {relative}")
        if change["old_sha256"] is not None:
            saved = child(backup, relative)
            if not saved.is_file() or digest(saved) != change["old_sha256"]:
                raise ValueError(f"rollback backup hash mismatch: {relative}")
    if len(changes) < 3 or [c["path"] for c in changes[-2:]] != [PROVENANCE, MANIFEST]:
        raise ValueError("rollback manifests must be last")
    if any(c["old_sha256"] is None for c in changes[-2:]):
        raise ValueError("rollback manifest backups are required")
    new_parents = [child(formal, c["path"]).parent for c in changes if c["old_sha256"] is None]
    directories = report.get("created_directories", [])
    if not isinstance(directories, list) or len(directories) != len(set(directories)):
        raise ValueError("invalid rollback directory inventory")
    for relative in directories:
        directory = child(formal, relative)
        if not any(parent == directory or parent.is_relative_to(directory) for parent in new_parents):
            raise ValueError("invalid rollback directory")
    assert_stopped(formal)
    if data_inventory(formal) != report["data"]:
        raise ValueError("user data inventory changed; rollback stopped")
    # Restore sources first and the runtime manifest last.
    for change in changes:
        assert_stopped(formal)
        target = child(formal, change["path"])
        if target.exists() and not target.is_file():
            raise ValueError(f"rollback target is not a file: {change['path']}")
        current = digest(target) if target.is_file() else None
        if current not in {change["old_sha256"], change["new_sha256"]}:
            raise ValueError(f"rollback target has changed: {change['path']}")
        if change["old_sha256"] is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_bytes(target, child(backup, change["path"]).read_bytes(), change["old_sha256"])
    for relative in sorted(report.get("created_directories", []), key=lambda p: len(p), reverse=True):
        assert_stopped(formal)
        directory = child(formal, relative)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    Bundle.load(child(formal, RUNTIME))
    if data_inventory(formal) != report["data"]:
        raise ValueError("user data inventory changed during rollback")
    report["status"] = "rolled_back"
    _write_report(backup, report)
    return report


def apply_plan(plan: dict, backup: Path) -> dict:
    repository = plain_path(Path(plan["repository"]))
    formal = formal_path(repository, Path(plan["formal"]))
    backup = backup_path(repository, formal, backup)
    assert_stopped(formal)
    if not plan["changes"]:
        return {"applied": False, "reason": "application sources already current"}
    if backup.exists():
        raise ValueError("fresh source backup directory required")
    # Reverify the entire prior runtime, and all new source hashes, before any
    # formal writes. Only the small replacement set is copied into backup.
    Bundle.load(child(formal, RUNTIME))
    created = set()
    for change in plan["changes"]:
        _check_current(formal, change, change["old_sha256"])
        if "source" in change and digest(child(repository, change["source"])) != change["new_sha256"]:
            raise ValueError(f"source changed after planning: {change['source']}")
        parent = child(formal, change["path"]).parent
        while parent != formal and not parent.exists():
            created.add(parent.relative_to(formal).as_posix())
            parent = parent.parent
    if data_inventory(formal) != plan["data"]:
        raise ValueError("user data inventory changed before promotion")
    backup.mkdir(parents=True)
    for change in plan["changes"]:
        if change["old_sha256"] is not None:
            saved = child(backup, change["path"])
            saved.parent.mkdir(parents=True, exist_ok=True)
            plain_path(saved)
            shutil.copy2(child(formal, change["path"]), saved)
            if digest(saved) != change["old_sha256"]:
                raise ValueError("source backup hash mismatch")
    report = {"schema": 1, "repository": str(repository), "formal": str(formal),
              "backup": str(backup), "changes": plan["changes"], "data": plan["data"],
              "created_directories": sorted(created), "status": "prepared"}
    _write_report(backup, report)
    try:
        for change in plan["changes"]:
            assert_stopped(formal)
            _check_current(formal, change, change["old_sha256"])
            if change["path"] == MANIFEST:
                content = json_bytes(plan["manifest"])
            elif change["path"] == PROVENANCE:
                content = json_bytes(plan["sources"])
            else:
                content = child(repository, change["source"]).read_bytes()
            _atomic_bytes(child(formal, change["path"]), content, change["new_sha256"])
        Bundle.load(child(formal, RUNTIME))
        if data_inventory(formal) != plan["data"]:
            raise ValueError("user data inventory changed during promotion")
        assert_stopped(formal)
        report.update(status="completed", data_unchanged=True,
                      completed_utc=datetime.now(timezone.utc).isoformat())
        _write_report(backup, report)
    except BaseException as failure:
        try:
            rollback_backup(repository, backup)
        except BaseException as recovery:
            raise RuntimeError(f"promotion failed ({failure}); rollback stopped ({recovery}); backup retained: {backup}") from failure
        raise
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--formal", type=Path, help="must equal repository/artifacts/releases/RecruitOps")
    parser.add_argument("--backup", type=Path, help="fresh small backup under artifacts/releases; required for --apply")
    parser.add_argument("--rollback", type=Path, help="restore a prior source backup; requires --apply")
    parser.add_argument("--apply", action="store_true", help="perform replacements after all managed processes exit normally")
    args = parser.parse_args(argv)
    if args.rollback:
        if not args.apply or args.backup or args.formal:
            parser.error("--rollback requires --apply and cannot be combined with --backup/--formal")
        result = rollback_backup(args.repository, args.rollback)
    else:
        if args.apply and args.backup is None:
            parser.error("--apply requires --backup")
        plan = make_plan(args.repository, args.formal)
        if args.apply:
            result = apply_plan(plan, args.backup)
        else:
            if args.backup:
                backup_path(Path(plan["repository"]), Path(plan["formal"]), args.backup)
            result = {key: value for key, value in plan.items() if key not in {"manifest", "sources"}}
            result.update(applied=False, ready_to_apply=not bool(plan["running_processes"]))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"source promotion refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
