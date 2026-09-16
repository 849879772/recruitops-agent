"""Import the legacy recruitment snapshot into Agent-owned storage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from packages.migration import LegacyMigrationError, run_migration  # noqa: E402


DEFAULT_DATABASE_URL = "postgresql+psycopg://recruitops:recruitops@localhost:5433/recruitops"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read config.yaml, jobs.db, and applications.json without source writes; "
            "export Agent companies and import existing snapshot tables."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read and validate only (default)")
    mode.add_argument("--apply", action="store_true", help="write Agent snapshot tables and companies.yaml")
    parser.add_argument("--mode", choices=("dry-run", "apply"), help="explicit migration mode")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=PROJECT_ROOT.parent,
        help="legacy project root; defaults to the parent of RecruitOps-Agent",
    )
    parser.add_argument(
        "--source-config", "--config", dest="source_config", type=Path,
        help="legacy config.yaml path",
    )
    parser.add_argument(
        "--source-database",
        "--source-db",
        "--jobs-db",
        dest="jobs_db",
        type=Path,
        help="legacy jobs.db path",
    )
    parser.add_argument(
        "--source-applications",
        "--applications-json",
        "--applications",
        dest="applications",
        type=Path,
        help="legacy applications.json path",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RECRUITOPS_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="existing Agent database URL; no schema is initialized by this tool",
    )
    parser.add_argument(
        "--companies-output",
        "--output",
        type=Path,
        default=PROJECT_ROOT / "config" / "companies.yaml",
        help="Agent-owned companies.yaml output path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode is not None and (args.dry_run or args.apply):
        print("--mode cannot be combined with --dry-run or --apply", file=sys.stderr)
        return 2
    mode = args.mode or ("apply" if args.apply else "dry-run")
    try:
        report = run_migration(
            source_root=args.source_root,
            source_config=args.source_config,
            jobs_db=args.jobs_db,
            applications=args.applications,
            database_url=args.database_url,
            companies_output=args.companies_output,
            mode=mode,
        )
    except (LegacyMigrationError, OSError, ValueError) as exc:
        print(f"legacy migration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
