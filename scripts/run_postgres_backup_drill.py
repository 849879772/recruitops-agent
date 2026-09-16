"""Run a local PostgreSQL backup and restore-preview drill."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.backup_drill import render_postgres_drill_report, run_postgres_backup_drill
from packages.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and verify a local PostgreSQL backup without restoring it."
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--database-url", default=None, help="Local PostgreSQL URL; never printed.")
    args = parser.parse_args()

    database_url = args.database_url or get_settings().database_url
    report = run_postgres_backup_drill(database_url, args.archive)
    print(render_postgres_drill_report(report))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
