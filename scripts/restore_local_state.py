from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.local_backup import restore_local_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or restore a local RecruitOps backup.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sqlite-database", type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply the restore; default is preview only.")
    args = parser.parse_args()
    manifest = restore_local_backup(
        args.archive,
        ROOT,
        sqlite_database=args.sqlite_database,
        apply=args.apply,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
