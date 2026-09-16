"""Build a read-only company acceptance view from replay and retry evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import unicodedata
from typing import Iterable, Mapping


BUCKET_FILES = {
    "new-failures": "detail_failure_count",
    "missing-jd": "detail_pending_count",
    "existing-repairs": "existing_detail_pending_count",
}


def _key(row: Mapping[str, object]) -> tuple[str, str]:
    title = unicodedata.normalize("NFKC", str(row.get("title") or "")).strip().casefold()
    return str(row.get("company_id") or ""), title


def _jsonl(path: Path) -> Iterable[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _successful_retries(paths: Iterable[Path]) -> dict[tuple[str, str], dict[str, object]]:
    successes: dict[tuple[str, str], dict[str, object]] = {}
    for directory in paths:
        for path in sorted(directory.glob("result-*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("success") is True:
                successes[_key(row)] = {
                    "company_id": row.get("company_id"),
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "evidence": str(path.resolve()),
                }
    return successes


def build_acceptance(replay: Path, retry_dirs: Iterable[Path]) -> tuple[list[dict], dict]:
    companies = [
        dict(row)
        for row in _jsonl(replay / "company-status.jsonl")
        if str(row.get("status") or "") != "unusable"
        and str(row.get("list_status") or "") != "unusable"
    ]
    by_company = {str(row.get("company_id") or ""): row for row in companies}
    buckets = {
        name: {_key(row): row for row in _jsonl(replay / f"{name}.jsonl")}
        for name in BUCKET_FILES
    }
    successes = _successful_retries(retry_dirs)
    recovered = Counter()

    for bucket_name, rows in buckets.items():
        field = BUCKET_FILES[bucket_name]
        for company_id, title_key in rows.keys() & successes.keys():
            company = by_company.get(company_id)
            if company is None:
                continue
            company[field] = max(0, int(company.get(field) or 0) - 1)
            recovered[bucket_name] += 1

    for company in companies:
        unresolved_failures = sum(
            int(company.get(field) or 0)
            for field in (
                "detail_failure_count",
                "existing_detail_failure_count",
                "existing_unrediscovered_failure_count",
            )
        )
        unresolved_pending = sum(
            int(company.get(field) or 0)
            for field in (
                "detail_pending_count",
                "existing_detail_pending_count",
                "existing_unrediscovered_pending_count",
            )
        )
        status = "partial" if unresolved_failures else str(company.get("list_status") or "partial")
        if not unresolved_failures and unresolved_pending and status == "complete":
            status = "partial"
        company["status"] = status
        company["detail_status"] = (
            "failed" if unresolved_failures else "pending" if unresolved_pending else "complete"
        )
        company["unresolved_detail_failure_count"] = unresolved_failures
        company["unresolved_detail_pending_count"] = unresolved_pending

    report = {
        "schema": "title-first-company-acceptance.v1",
        "read_only": True,
        "formal_database_writes": 0,
        "model_calls": 0,
        "company_units": len(companies),
        "source_records": sum(int(row.get("source_count") or 0) for row in companies),
        "statuses": dict(Counter(str(row.get("status")) for row in companies)),
        "list_statuses": dict(Counter(str(row.get("list_status")) for row in companies)),
        "detail_statuses": dict(Counter(str(row.get("detail_status")) for row in companies)),
        "recovered": dict(recovered),
        "unique_successful_retries": len(successes),
        "inputs": {
            "replay": str(replay.resolve()),
            "company_status_sha256": sha256((replay / "company-status.jsonl").read_bytes()).hexdigest(),
            "retry_dirs": [str(path.resolve()) for path in retry_dirs],
        },
    }
    return companies, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--retry-dir", action="append", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite an existing acceptance directory")
    companies, report = build_acceptance(args.replay, args.retry_dir)
    args.output.mkdir(parents=True)
    with (args.output / "company-status.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in companies:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (args.output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
