from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.local_backup import create_local_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up local RecruitOps state without secrets.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sqlite-database", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / ".data" / "backups" / f"recruitops-{datetime.now():%Y%m%d-%H%M%S}.zip"
    print(create_local_backup(ROOT, output, sqlite_database=args.sqlite_database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
