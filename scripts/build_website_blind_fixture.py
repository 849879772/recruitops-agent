from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def _integer(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def build_fixture(source: Path, destination: Path, *, count: int = 30) -> None:
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    eligible = [
        row
        for row in rows
        if row.get("status") == "HEALTHY"
        and _integer(row.get("raw_jobs")) > 0
        and str(row.get("careers_url") or "").startswith(("http://", "https://"))
    ]
    eligible.sort(
        key=lambda row: hashlib.sha256(
            f"{row.get('company')}\0{row.get('careers_url')}".encode("utf-8")
        ).hexdigest()
    )
    selected = []
    seen_companies: set[str] = set()
    for row in eligible:
        company_key = str(row.get("company") or "").casefold()
        if not company_key or company_key in seen_companies:
            continue
        seen_companies.add(company_key)
        selected.append(row)
        if len(selected) == count:
            break
    if len(selected) < count:
        raise RuntimeError(f"only {len(selected)} eligible audit rows are available")

    fixture = {
        "version": 1,
        "source_snapshot": source.name,
        "selection": "sha256(company + source_url), healthy non-empty rows",
        "development_exclusion": (
            "The fixture is frozen after implementation and must not be changed while "
            "tuning crawler acceptance logic."
        ),
        "cases": [
            {
                "case_id": f"blind-{index:02d}",
                "company": row["company"],
                "crawler": row["crawler"],
                "source_url": row["careers_url"],
                "expected_jobs": _integer(row.get("formal_jobs")),
                "expected_detail_jobs": _integer(row.get("detail_jobs")),
                "expected_status": row["status"],
                "frozen_at": row["checked_at"],
            }
            for index, row in enumerate(selected, start=1)
        ],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()
    build_fixture(args.source, args.destination, count=args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
