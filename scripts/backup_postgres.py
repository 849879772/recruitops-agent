from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.postgres_backup import create_postgres_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified local PostgreSQL backup.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / ".data" / "backups" / f"postgres-{datetime.now():%Y%m%d-%H%M%S}.dump"
    manifest = create_postgres_backup(get_settings().database_url, output)
    print(f"created {manifest['archive']} sha256={manifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
