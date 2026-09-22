"""Assemble real downloaded native inputs and public wheels, with full inventory."""

import argparse
import email
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import Bundle
from packages.desktop_runtime.staging import digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application-source", required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if not destination.is_relative_to(ROOT / ".desktop-runtime-tests") or destination.exists():
        raise ValueError("fresh repository-local bundle required")
    build = ROOT / ".desktop-runtime-tests/native-build"
    destination.mkdir(parents=True)
    for name in ("python", "postgres", "codex", "chromium"):
        shutil.copytree(build / name, destination / name, ignore=shutil.ignore_patterns(".links"))
    for name in ("server_license.txt", "commandlinetools_3rd_party_licenses.txt"):
        with zipfile.ZipFile(build / "postgres-16.15.zip") as source:
            (destination / "postgres" / name).write_bytes(source.read("pgsql/" + name))
    licenses = destination / "licenses"
    licenses.mkdir()
    shutil.copyfile(build / "pgvector/LICENSE", licenses / "pgvector.txt")
    node = ROOT / ".desktop-runtime-tests/native-components-smoke-v2/node"
    (destination / "node").mkdir()
    for name in ("node.exe", "LICENSE"):
        shutil.copyfile(node / name, destination / "node" / name)
    wheels = sorted((build / "wheels").glob("*.whl"))
    pinned = json.loads((ROOT / "scripts/desktop/native-wheels.lock.json").read_text(encoding="utf-8"))
    expected = {item["filename"]: item["sha256"] for item in pinned["wheels"]}
    if {wheel.name for wheel in wheels} != set(expected):
        raise ValueError("wheel inventory does not match the recorded lock")
    if any(digest(wheel) != expected[wheel.name] for wheel in wheels):
        raise ValueError("pinned wheel hash mismatch")
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile",
                    "--target", str(destination / "python/Lib/site-packages"), *map(str, wheels)], check=True)
    shutil.copyfile(ROOT / "scripts/desktop/python311._pth", destination / "python/python311._pth")
    # EDB's Windows PostgreSQL binaries require the VC runtime. Keep an app-local
    # copy so first-run initdb also works on clean Windows installations.
    shutil.copyfile(destination / "python/vcruntime140.dll", destination / "postgres/bin/vcruntime140.dll")
    source_program = "process.stdout.write(JSON.stringify(require('./apps/desktop/packaging/resources.cjs').sourceInventory(process.cwd())))"
    source_files = json.loads(subprocess.check_output([str(node / "node.exe"), "-e", source_program], cwd=ROOT))
    if not any(name.endswith(".sql") for name in source_files):
        raise ValueError("source inventory omitted migrations")
    for relative in source_files:
        target = destination / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    provenance = destination / "provenance"
    provenance.mkdir()
    for name in ("acquisition.json", "codex-acquisition.json", "wheels.json"):
        shutil.copyfile(build / name, provenance / name)
    (provenance / "application-files.json").write_text(json.dumps(source_files, indent=2), encoding="utf-8")
    package_records = []
    site = destination / "python/Lib/site-packages"
    for metadata_path in sorted(site.glob("*.dist-info/METADATA")):
        metadata = email.message_from_bytes(metadata_path.read_bytes())
        notices = [p.relative_to(destination).as_posix() for p in metadata_path.parent.rglob("*")
                   if p.is_file() and any(s in p.name.lower() for s in ("license", "copying", "notice"))]
        package_records.append({"name": metadata["Name"], "version": metadata["Version"],
            "license_expression": metadata.get("License-Expression") or metadata.get("License"), "notice_files": notices})
    (provenance / "python-licenses.json").write_text(json.dumps(package_records, indent=2), encoding="utf-8")
    entries = {name: f"postgres/bin/{name}.exe" for name in (
        "postgres", "initdb", "psql", "pg_dump", "pg_restore", "pg_ctl", "pg_config")}
    entries.update(python="python/python.exe", node="node/node.exe", codex="codex/codex-x86_64-pc-windows-msvc.exe",
        chromium="chromium/chromium-1208/chrome-win64/chrome.exe", vector_dll="postgres/lib/vector.dll",
        vector_control="postgres/share/extension/vector.control", vector_sql="postgres/share/extension/vector--0.8.1.sql",
        api_bootstrap="app/packages/desktop_runtime/api_bootstrap.py", migration_script="app/scripts/apply_migrations.py")
    acquired = json.loads((build / "acquisition.json").read_text())
    components = {name: {"version": acquired[name]["version"], "source": acquired[name]["source"], "license_file": license_file}
        for name, license_file in (("python", "python/LICENSE.txt"), ("postgres", "postgres/server_license.txt"), ("pgvector", "licenses/pgvector.txt"))}
    application_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    components.update(
        node={"version": "22.14.0", "source": "https://nodejs.org/dist/v22.14.0/", "license_file": "node/LICENSE"},
        codex={"version": "0.149.0", "source": "https://github.com/openai/codex/releases/tag/rust-v0.149.0", "license_file": "codex/LICENSE"},
        chromium={"version": "145.0.7632.6", "source": "https://cdn.playwright.dev/chrome-for-testing-public/145.0.7632.6/win64/chrome-win64.zip", "license_file": "chromium/chromium-1208/chrome-win64/ABOUT"},
        application={"version": application_version, "source": args.application_source, "license_file": "app/LICENSE"})
    manifest = {"schema": 1, "platform": "windows-x64", "postgres_major": 16,
        "components": components, "entrypoints": entries,
        "files": {p.relative_to(destination).as_posix(): digest(p) for p in sorted(destination.rglob("*")) if p.is_file()}}
    Bundle(destination, manifest).verify()
    (destination / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"bundle": str(destination), "files": len(manifest["files"]), "release_accepted": False}))


if __name__ == "__main__":
    main()
