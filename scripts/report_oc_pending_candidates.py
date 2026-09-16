"""Build a deterministic work queue for OC companies that still need integration."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.tools.oc_candidates import OcCandidateListInput, OcCandidateRunner


def build_report(snapshot: Path, companies: Path) -> dict[str, object]:
    runner = OcCandidateRunner(snapshot, companies)
    first = runner.list(OcCandidateListInput(limit=100))
    if not first.success or first.data is None:
        raise RuntimeError(first.error_message or "unable to build OC candidate queue")
    items = list(first.data.candidates)
    for offset in range(100, first.data.total_candidates, 100):
        page = runner.list(OcCandidateListInput(offset=offset, limit=100))
        if not page.success or page.data is None:
            raise RuntimeError(page.error_message or "unable to page OC candidate queue")
        items.extend(page.data.candidates)
    entry_counts: Counter[str] = Counter()
    crawler_counts: Counter[str] = Counter()
    rows: list[dict[str, object]] = []
    for item in items:
        diagnosis = item.entry_diagnoses[0] if item.entry_diagnoses else None
        entry_kind = diagnosis.entry_kind if diagnosis else "no_public_url"
        crawler_key = diagnosis.crawler_key if diagnosis and diagnosis.crawler_key else "none"
        entry_counts[entry_kind] += 1
        crawler_counts[crawler_key] += 1
        rows.append({
            "company": item.company,
            "source_projects": item.source_projects,
            "source_urls": [entry.url for entry in item.entry_diagnoses],
            "entry_kind": entry_kind,
            "crawler_key": None if crawler_key == "none" else crawler_key,
            "candidate_kind": diagnosis.candidate_kind if diagnosis else None,
            "approval_state": item.approval_state,
            "runtime_enabled": False,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_captured_at": first.data.snapshot_captured_at,
        "policy": "OC-only; deterministic classification; no model analysis; no runtime enablement",
        "total": first.data.total_candidates,
        "addressable": first.data.resolved_candidates,
        "entry_discovery_required": first.data.unresolved_candidates,
        "entry_kind_counts": dict(sorted(entry_counts.items())),
        "crawler_counts": dict(sorted(crawler_counts.items())),
        "candidates": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=ROOT / ".data/discovery/givemeoc_latest.json")
    parser.add_argument("--companies", type=Path, default=ROOT / "config/companies.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / ".data/evals/oc_pending_candidates.json")
    args = parser.parse_args()
    report = build_report(args.snapshot, args.companies)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "total", "addressable", "entry_discovery_required", "entry_kind_counts", "crawler_counts"
    )}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
