"""Apply four reviewed scoring files to the stopped formal desktop; dry-run by default."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import Bundle  # noqa: E402
from packages.desktop_runtime.staging import digest  # noqa: E402


FILES = (
    "packages/matching/service.py",
    "packages/matching/resume.py",
    "packages/storage/sync.py",
    "packages/pipeline/daily.py",
)
PROVENANCE = "provenance/application-files.json"
MANIFEST = "runtime-manifest.json"


def formal_processes(formal: Path) -> list[dict]:
    """Read all Windows processes without displaying command lines or stopping any."""
    if os.name != "nt":
        raise RuntimeError("windows_process_check_required")
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$prefix = $env:RECRUITOPS_FORMAL_UPDATE_ROOT.TrimEnd('\') + '\'
$found = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.Replace('/', '\').StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine.Replace('/', '\').IndexOf($prefix, [StringComparison]::OrdinalIgnoreCase) -ge 0)
} | Select-Object ProcessId, Name, ExecutablePath)
ConvertTo-Json -InputObject $found -Compress
"""
    env = {**os.environ, "RECRUITOPS_FORMAL_UPDATE_ROOT": str(formal)}
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout.decode("utf-8-sig"))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise RuntimeError("formal_process_check_failed") from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise RuntimeError("formal_process_check_invalid")
    return data


def _replace_bytes(path: Path, content: bytes) -> None:
    """Replace one file atomically and remove temporary files on failure."""
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def deploy(
    repository: Path = ROOT,
    *,
    apply: bool = False,
    process_probe: Callable[[Path], list[dict]] | None = None,
) -> dict:
    repository = repository.resolve()
    releases = repository / "artifacts" / "releases"
    formal = releases / "RecruitOps"
    runtime = formal / "resources" / "desktop-runtime"
    # Refuse junctions/symlinks redirecting the fixed installation or source files.
    for path in (releases, formal, runtime.parent, runtime):
        if path.resolve() != path:
            raise RuntimeError("unexpected_formal_path")
    bundle = Bundle.load(runtime)
    original_manifest = (runtime / MANIFEST).read_bytes()
    provenance_bytes = (runtime / PROVENANCE).read_bytes()
    inventory = json.loads(provenance_bytes)
    app_files = {name[4:] for name in bundle.manifest["files"] if name.startswith("app/")}
    if (not isinstance(inventory, list) or any(not isinstance(item, str) for item in inventory)
            or len(inventory) != len(set(inventory)) or set(inventory) != app_files):
        raise RuntimeError("application_provenance_mismatch")
    if PROVENANCE not in bundle.manifest["files"]:
        raise RuntimeError("application_provenance_untracked")

    source_bytes = {}
    for relative in FILES:
        source = repository / relative
        if source.resolve() != source or relative not in app_files:
            raise RuntimeError("hotfix_source_path_or_inventory_invalid")
        content = source.read_bytes()
        compile(content, str(source), "exec")
        source_bytes[relative] = content
    changed = [
        relative for relative, content in source_bytes.items()
        if hashlib.sha256(content).hexdigest() != bundle.manifest["files"]["app/" + relative]
    ]
    probe = process_probe or formal_processes
    running = probe(formal)
    report = {
        "mode": "apply" if apply else "dry_run",
        "formal": str(formal),
        "files": list(FILES),
        "changed": changed,
        "running_processes": running,
        "ready": not running,
        "updated": [],
        "data_untouched": True,
    }
    if not apply:
        return report
    if running:
        raise RuntimeError("formal_processes_running")
    if not changed:
        report["already_current"] = True
        return report

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(tempfile.mkdtemp(prefix=f"RecruitOps-before-matching-fix-{stamp}-", dir=releases))
    for relative in FILES:
        target = backup / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(runtime / "app" / relative, target)
    shutil.copytree(runtime / "provenance", backup / "provenance")
    shutil.copy2(runtime / MANIFEST, backup / MANIFEST)
    if ((backup / MANIFEST).read_bytes() != original_manifest
            or (backup / PROVENANCE).read_bytes() != provenance_bytes
            or any(digest(backup / "app" / relative) != bundle.manifest["files"]["app/" + relative]
                   for relative in FILES)):
        raise RuntimeError(f"matching_fix_backup_changed; backup={backup}")
    # Scan again after backup, immediately before modifying the installation.
    if probe(formal):
        raise RuntimeError(f"formal_processes_started_before_update; backup={backup}")

    manifest = json.loads(original_manifest)
    try:
        for relative, content in source_bytes.items():
            target = runtime / "app" / relative
            _replace_bytes(target, content)
            expected = hashlib.sha256(content).hexdigest()
            if digest(target) != expected:
                raise RuntimeError("matching_fix_source_copy_mismatch")
            manifest["files"]["app/" + relative] = expected
        # This hotfix neither adds nor removes sources, so provenance stays byte-identical.
        manifest["files"][PROVENANCE] = digest(runtime / PROVENANCE)
        Bundle(runtime, manifest).verify()
        _replace_bytes(runtime / MANIFEST, json.dumps(manifest, indent=2).encode("utf-8"))
        Bundle.load(runtime)
    except BaseException:
        try:
            for relative in FILES:
                _replace_bytes(runtime / "app" / relative, (backup / "app" / relative).read_bytes())
            _replace_bytes(runtime / PROVENANCE, provenance_bytes)
            _replace_bytes(runtime / MANIFEST, original_manifest)
            Bundle.load(runtime)
        except BaseException as exc:
            raise RuntimeError(f"matching_fix_rollback_failed; backup={backup}") from exc
        raise
    report.update(updated=list(FILES), backup=str(backup), manifest_sha256=digest(runtime / MANIFEST))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply after checking the formal desktop is stopped")
    mode.add_argument("--dry-run", action="store_true", help="Read-only verification (default)")
    args = parser.parse_args(argv)
    try:
        report = deploy(apply=args.apply)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "applied": False}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
