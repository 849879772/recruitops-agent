from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.postgres_backup import restore_postgres_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or restore a local PostgreSQL backup.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = restore_postgres_backup(
        get_settings().database_url,
        args.archive,
        apply=args.apply,
    )
    print(f"restore_status={result['restore_status']} archive={result['archive']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
