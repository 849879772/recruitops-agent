"""Import verified offline gpt-5.6-luna review files into Agent snapshots."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings  # noqa: E402
from packages.matching.review_import import (  # noqa: E402
    ReviewImportError,
    import_reviewed_scores_from_files,
)
from packages.matching.rules import profile_fingerprint  # noqa: E402
from packages.storage import Storage  # noqa: E402


DEFAULT_DATABASE_URL = "postgresql+psycopg://recruitops:recruitops@localhost:5433/recruitops"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--result",
        dest="result_files",
        action="append",
        default=[],
        type=Path,
        help="one Luna result JSON; repeat for multiple files",
    )
    parser.add_argument(
        "--results",
        dest="result_groups",
        action="append",
        nargs="+",
        default=[],
        type=Path,
        help="one or more Luna result JSON files",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy database URL; defaults to RECRUITOPS_DATABASE_URL or settings",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and plan only (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="verify the frozen backup and write one transaction",
    )
    parser.add_argument(
        "--replace-complete-job-id",
        action="append",
        default=[],
        help="allow replacement of one frozen old complete analysis; repeatable",
    )
    parser.add_argument(
        "--replace-complete-job-ids",
        default="",
        help="comma-separated complete-analysis replacement whitelist",
    )
    return parser


def _current_profile_fingerprint(settings: Any) -> str:
    path = Path(settings.candidate_profile_config).expanduser()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ReviewImportError(f"current_profile_unreadable:{path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("profile"), dict):
        raise ReviewImportError("current_profile_invalid")
    return profile_fingerprint(payload["profile"])


def _replace_ids(args: argparse.Namespace) -> list[str]:
    result = [str(value).strip() for value in args.replace_complete_job_id if str(value).strip()]
    result.extend(
        item.strip()
        for item in str(args.replace_complete_job_ids or "").split(",")
        if item.strip()
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result_paths = list(args.result_files)
    for group in args.result_groups:
        result_paths.extend(group)
    if not result_paths:
        print("at least one --result/--results file is required", file=sys.stderr)
        return 2

    settings = get_settings()
    database_url = (
        args.database_url
        or os.environ.get("RECRUITOPS_DATABASE_URL")
        or settings.database_url
        or DEFAULT_DATABASE_URL
    )
    current_profile_digest = _current_profile_fingerprint(settings) if args.apply else None
    storage = Storage.from_url(database_url)
    try:
        report = import_reviewed_scores_from_files(
            storage,
            args.manifest,
            result_paths,
            apply=args.apply,
            replace_complete_job_ids=_replace_ids(args),
            current_profile_fingerprint=current_profile_digest,
        )
    except ReviewImportError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        storage.engine.dispose()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
