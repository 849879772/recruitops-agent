"""Refresh only allowlisted application source before handing off a native bundle."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import Bundle
from packages.desktop_runtime.staging import digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    root = args.bundle.resolve()
    if not root.is_relative_to(ROOT / ".desktop-runtime-tests"):
        raise ValueError("isolated bundle required")
    manifest_path = root / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    script = "process.stdout.write(JSON.stringify(require('./apps/desktop/packaging/resources.cjs').sourceInventory(process.cwd())))"
    files = json.loads(subprocess.check_output([str(root / "node/node.exe"), "-e", script], cwd=ROOT))
    if set(p[4:] for p in manifest["files"] if p.startswith("app/")) - set(files):
        raise ValueError("removed source files require fresh assembly")
    changed = []
    for relative in files:
        actual = digest(ROOT / relative)
        if manifest["files"].get("app/" + relative) != actual:
            target = root / "app" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
            manifest["files"]["app/" + relative] = actual
            changed.append(relative)
    provenance = root / "provenance/application-files.json"
    provenance.write_text(json.dumps(files, indent=2), encoding="utf-8")
    manifest["files"]["provenance/application-files.json"] = digest(provenance)
    # Invalid hashes during refresh fail closed; never expose a partially refreshed manifest.
    Bundle(root, manifest).verify()
    temporary = root / "runtime-manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    print(json.dumps({"refreshed_app_sources": changed, "manifest_sha256": digest(manifest_path)}))


if __name__ == "__main__":
    main()
