"""Run one fixed RecruitOps task through the local read-only scheduler."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.scheduler import (  # noqa: E402
    DEFAULT_LOCK_PATH,
    DEFAULT_TASKS,
    LocalTaskScheduler,
    RunStatus,
    TaskCallable,
    build_runtime_task_handlers,
)


def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 local or offset-aware timestamp."""

    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO datetime: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=sorted(DEFAULT_TASKS))
    parser.add_argument(
        "--now",
        type=parse_datetime,
        help="observation time in ISO-8601 form; defaults to the local clock",
    )
    parser.add_argument(
        "--scheduled-for",
        type=parse_datetime,
        help="override the expected schedule time, useful for explicit catch-up runs",
    )
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-retries", type=int)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=ROOT / DEFAULT_LOCK_PATH,
        help="local scheduler lock path; this is not a business database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the run plan without invoking a handler or acquiring the lock",
    )
    return parser


def run_local_task(
    argv: Sequence[str] | None = None,
    *,
    handlers: Mapping[str, TaskCallable] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    handler_map = handlers or build_runtime_task_handlers()
    scheduler = LocalTaskScheduler(lock_path=args.lock_path)
    result = scheduler.run(
        args.task,
        handler_map.get(args.task),
        now=args.now,
        scheduled_for=args.scheduled_for,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        run_id=args.run_id,
        dry_run=args.dry_run,
    )
    # Keep the machine-readable CLI portable across Windows code pages.
    print(json.dumps(result.to_dict(), ensure_ascii=True, sort_keys=True))

    if result.status in {RunStatus.SUCCESS, RunStatus.DRY_RUN, RunStatus.SKIPPED_LOCKED}:
        return 0
    if result.status is RunStatus.TIMED_OUT:
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_local_task(argv)
    except (TypeError, ValueError, OSError) as exc:
        print(f"local task was not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
