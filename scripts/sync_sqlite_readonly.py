"""Read the autumn SQLite/JSON source and sync Agent-owned snapshots only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings  # noqa: E402
from packages.repositories.autumn_source import AutumnSourceRepository  # noqa: E402
from packages.security.source_guard import ensure_target_outside_source_root  # noqa: E402
from packages.storage import SnapshotSyncService, Storage  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def _guard_agent_database(source_root: Path, database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return
    target = Path(url.database)
    if not target.is_absolute():
        target = ROOT / target
    ensure_target_outside_source_root(source_root, target, allowed_root=ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize the read-only autumn source into Agent-owned storage."
    )
    parser.add_argument("--source-root", type=Path, help="autumn-system root containing data/")
    parser.add_argument(
        "--database-url",
        help="Agent-owned database URL; defaults to RECRUITOPS_DATABASE_URL",
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--hydrate-job-details",
        action="store_true",
        help="read each job detail from the source before writing snapshots",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    defaults = Settings()
    settings = Settings(
        source_root=args.source_root or defaults.source_root,
        database_url=args.database_url or defaults.database_url,
    )
    _guard_agent_database(settings.source_root, settings.database_url)

    repository = AutumnSourceRepository(settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    try:
        result = SnapshotSyncService(
            storage,
            repository,
            page_size=args.page_size,
        ).sync_all(hydrate_job_details=args.hydrate_job_details)
    finally:
        storage.engine.dispose()

    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
