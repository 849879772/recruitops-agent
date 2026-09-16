"""Import a public snapshot into source records, never jobs or scoring."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.offerbiu_registry import import_offerbiu_sources
from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if not args.apply:
        print(json.dumps({"dry_run": True, "records": len(payload.get("items", []))}))
        return
    storage = Storage.from_url(args.database_url)
    try:
        result = import_offerbiu_sources(CompanySourceRegistry(storage), payload)
        result.pop("ids", None)
        print(json.dumps(result))
    finally:
        storage.engine.dispose()


if __name__ == "__main__":
    main()
