"""Run deterministic offline OfferBiu screening and read-only catalog reconciliation.

Screening:
    python scripts/screen_offerbiu_capture.py screen \
      --input .data/evals/offerbiu_full_20260908_v1/hydration/jobs.jsonl \
      --output-dir .data/evals/offerbiu_screening_20260909/screen \
      --profile config/candidate_profile.yaml

Reconciliation reads only the supplied JSON snapshot.  The smallest supported
snapshot shape is:

    {
      "companies": [{"id": "...", "name": "...", "aliases": []}],
      "jobs": [{"id": "...", "company_id": "...", "title": "...",
                 "detail_url": "...", "native_job_id": "...",
                 "business_key": "...", "jd_raw": "..."}],
      "analyses": [{"job_id": "...", "analysis_status": "...",
                    "match_score": null, "content_fingerprint": "..."}],
      "application_audit": {"count": 0, "digest": "..."},
      "read_only": true
    }

Legacy jobs may omit business_key or native_job_id.  They are matched only by
the remaining controlled identities; title similarity is never a merge key.
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

from packages.candidate_profile import load_candidate_profile
from packages.matching.capture_screening import (
    SNAPSHOT_CONTRACT,
    reconcile_passed_jsonl,
    screen_capture_jsonl,
)


DEFAULT_INPUT = ROOT / ".data" / "evals" / "offerbiu_full_20260908_v1" / "hydration" / "jobs.jsonl"
DEFAULT_PROFILE = ROOT / "config" / "candidate_profile.yaml"


def _screen(args: argparse.Namespace) -> dict[str, Any]:
    profile = load_candidate_profile(Path(args.profile))
    return screen_capture_jsonl(Path(args.input), Path(args.output_dir), profile)


def _reconcile(args: argparse.Namespace) -> dict[str, Any]:
    return reconcile_passed_jsonl(
        Path(args.passed),
        Path(args.snapshot),
        Path(args.output_dir),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    screen = subparsers.add_parser("screen", help="screen frozen hydration JSONL without model or network")
    screen.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    screen.add_argument("--output-dir", type=Path, required=True)
    screen.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    screen.set_defaults(handler=_screen)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="reconcile passed-only JSONL against a read-only catalog JSON snapshot",
    )
    reconcile.add_argument("--passed", type=Path, required=True)
    reconcile.add_argument("--snapshot", type=Path, required=True)
    reconcile.add_argument("--output-dir", type=Path, required=True)
    reconcile.set_defaults(handler=_reconcile)

    contract = subparsers.add_parser("snapshot-contract", help="print the accepted snapshot field contract")
    contract.set_defaults(handler=lambda _args: SNAPSHOT_CONTRACT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = args.handler(args)
    if args.command == "screen":
        output = {
            "mode": summary.get("mode"),
            "records_read": summary.get("records_read"),
            "counts": summary.get("counts"),
            "malformed_lines": summary.get("malformed_lines"),
            "candidate_used_records": summary.get("candidate_used_records"),
            "files": summary.get("files"),
        }
    elif args.command == "reconcile":
        output = {
            "mode": summary.get("mode"),
            "records_read": summary.get("records_read"),
            "counts": summary.get("counts"),
            "source_row_counts": summary.get("source_row_counts"),
            "distinct_job_counts": summary.get("distinct_job_counts"),
            "potential_scoring_work": summary.get("potential_scoring_work"),
            "batch_company_identity_conflicts": summary.get("batch_company_identity_conflicts"),
            "changed_report": summary.get("changed_report"),
            "malformed_records": summary.get("malformed_records"),
            "snapshot_counts": summary.get("snapshot_counts"),
            "files": summary.get("files"),
        }
    else:
        output = summary
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
