"""Build isolated pinned resources; partial inventories are not launchable bundles."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.instance import reject_links
from packages.desktop_runtime.staging import assemble_bundle, package_bundle, stage_components


def isolated(path):
    raw = Path(path).absolute()
    resolved = raw.resolve()
    boundary = ROOT / ".desktop-runtime-tests"
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise RuntimeFailure("isolated_build_path_required")
    for parent in (raw, *raw.parents):
        if parent.exists() and (parent.is_symlink() or (os.name == "nt" and parent.lstat().st_file_attributes & 0x400)):
            raise RuntimeFailure("linked_build_path")
    if resolved.exists():
        reject_links(resolved)
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--lock", required=True, type=Path)
    stage.add_argument("--cache", required=True, type=Path)
    stage.add_argument("--output", required=True, type=Path)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--plan", required=True, type=Path)
    assemble.add_argument("--inputs", required=True, type=Path)
    assemble.add_argument("--output", required=True, type=Path)
    package = commands.add_parser("package")
    package.add_argument("--bundle", required=True, type=Path)
    package.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        output = isolated(args.output)
        if args.command == "stage":
            result = stage_components(json.loads(args.lock.read_text()), isolated(args.cache), output)
            result = {"staged_components": sorted(result["components"]), "release_accepted": False}
        elif args.command == "assemble":
            assemble_bundle(json.loads(args.plan.read_text()), isolated(args.inputs), output)
            result = {"bundle_verified": True, "release_accepted": False}
        else:
            result = package_bundle(isolated(args.bundle), output)
        print(json.dumps(result))
        return 0
    except (RuntimeFailure, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": exc.code if isinstance(exc, RuntimeFailure) else "staging_io_or_contract_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
