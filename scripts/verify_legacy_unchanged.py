"""Create and verify the read-only manifest for the legacy source checkout."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.security.source_guard import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    SourceChangedError,
    SourceGuardError,
    snapshot_source,
    verify_source_unchanged,
)


def _default_source_root() -> Path:
    return Path(os.environ.get("RECRUITOPS_SOURCE_ROOT", "D:/秋招系统"))


def _default_manifest_path() -> Path:
    configured = os.environ.get("RECRUITOPS_SOURCE_MANIFEST")
    if not configured:
        return DEFAULT_MANIFEST_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path


def _manifest_path(value: Path | None) -> Path:
    if value is None:
        return _default_manifest_path()
    return value if value.is_absolute() else ROOT / value


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="legacy source checkout; defaults to RECRUITOPS_SOURCE_ROOT",
    )
    parser.add_argument(
        "--manifest",
        "--output",
        dest="manifest",
        type=Path,
        default=None,
        help="Agent-owned manifest path; defaults to Agent .data",
    )
    parser.add_argument(
        "--agent-root",
        type=Path,
        default=ROOT,
        help="Agent root to exclude when nested under the source root",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot or verify the legacy source without writing to it."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="write a fresh source manifest")
    _add_common_options(snapshot)
    verify = commands.add_parser("verify", help="verify the source against its manifest")
    _add_common_options(verify)
    return parser


def _summary(command: str, source_root: Path, manifest_path: Path, manifest: dict) -> str:
    summary = {
        "command": command,
        "source_root": str(source_root.resolve()),
        "manifest": str(manifest_path.resolve()),
        "file_count": manifest.get("file_count"),
        "aggregate_sha256": manifest.get("aggregate_sha256"),
    }
    if command == "verify":
        summary["unchanged"] = True
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source_root = (args.source_root or _default_source_root()).expanduser()
    manifest_path = _manifest_path(args.manifest)
    try:
        if args.command == "snapshot":
            manifest = snapshot_source(
                source_root,
                manifest_path,
                agent_root=args.agent_root,
            )
            print(_summary("snapshot", source_root, manifest_path, manifest))
            return 0

        verify_source_unchanged(
            source_root,
            manifest_path=manifest_path,
            agent_root=args.agent_root,
        )
        manifest = json.loads(manifest_path.resolve().read_text(encoding="utf-8"))
        print(_summary("verify", source_root, manifest_path, manifest))
        return 0
    except SourceChangedError as exc:
        print(
            json.dumps(
                {
                    "command": "verify",
                    "manifest": str(manifest_path.resolve()),
                    "unchanged": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    except SourceGuardError as exc:
        print(f"source guard error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
