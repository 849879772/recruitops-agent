"""Replay frozen OfferBiu capture data with the title-first policy.

This command is offline and read-only.  It writes an independent output
directory containing source/company state, title-screened rows, detail reuse or
failure buckets, retry candidates, preserved applications, and a blocked
historical deactivation dry-run plan.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.matching.title_first_replay import replay


def _profile(path: Path | None) -> Any:
    if path is None:
        return None
    try:
        from packages.candidate_profile import load_candidate_profile

        # Keep CLI profile parsing identical to the formal pipeline loader.
        return load_candidate_profile(path)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid profile file: {path}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True, help="Frozen company sources.json")
    parser.add_argument(
        "--checkpoints",
        "--checkpoint-dir",
        dest="checkpoints",
        type=Path,
        required=True,
        help="Frozen list checkpoint directory",
    )
    parser.add_argument(
        "--hydration",
        "--hydration-jobs",
        dest="hydration",
        type=Path,
        required=True,
        help="Frozen hydration/jobs.jsonl or its directory",
    )
    parser.add_argument(
        "--catalog",
        "--catalog-snapshot",
        dest="catalog",
        type=Path,
        required=True,
        help="Read-only catalog snapshot",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, help="Optional title-policy profile fixture")
    parser.add_argument("--resume", action="store_true", help="Resume the same frozen input set")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = replay(
            args.sources,
            args.checkpoints,
            args.hydration,
            args.catalog,
            args.output_dir,
            profile=_profile(args.profile),
            resume=args.resume,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
