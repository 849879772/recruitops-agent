"""Assemble a deterministic, read-only import preview from title-first evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.reconciliation import normalize_company_name
from packages.matching.title_policy import normalize_job_title_key


def _jsonl(path: Path) -> Iterable[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _key(row: Mapping[str, object]) -> tuple[str, str]:
    return str(row.get("company_id") or ""), normalize_job_title_key(row.get("title"))


def _retry_results(paths: Iterable[Path]) -> dict[tuple[str, str], dict[str, object]]:
    result = {}
    for directory in paths:
        for path in sorted(directory.glob("result-*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            row["acceptance_evidence"] = str(path.resolve())
            key = _key(row)
            if result.get(key, {}).get("success") is True and row.get("success") is not True:
                continue
            result[key] = row
    return result


def _completed_job(base: Mapping[str, object], retry: Mapping[str, object]) -> dict[str, object]:
    job = dict(base)
    response = retry.get("response") if isinstance(retry.get("response"), Mapping) else {}
    detail = str(response.get("detail") or retry.get("detail") or "")
    evidence = retry.get("capture_evidence") if isinstance(retry.get("capture_evidence"), Mapping) else {}
    job.update(
        jd_raw=detail,
        jd_url=str(response.get("detail_url") or retry.get("detail_url") or job.get("jd_url") or ""),
        detail_url=str(response.get("detail_url") or retry.get("detail_url") or job.get("detail_url") or ""),
        capture_status="complete",
        capture_failure_reason=None,
        capture_evidence=dict(evidence),
        acceptance_evidence=retry.get("acceptance_evidence"),
    )
    return job


def _catalog_index(catalog: Mapping[str, object]) -> tuple[dict[tuple[str, str], list[dict]], dict[str, dict]]:
    companies = {
        str(row.get("id") or ""): row
        for row in catalog.get("companies", [])
        if isinstance(row, Mapping)
    }
    analyses = {
        str(row.get("job_id") or ""): dict(row)
        for row in catalog.get("analyses", [])
        if isinstance(row, Mapping)
    }
    index: dict[tuple[str, str], list[dict]] = {}
    for raw in catalog.get("jobs", []):
        if not isinstance(raw, Mapping):
            continue
        job = dict(raw)
        company = companies.get(str(job.get("company_id") or ""), {})
        names = {normalize_company_name(name) for name in [company.get("name"), *(company.get("aliases") or [])]}
        for company_key in names:
            if company_key:
                index.setdefault((company_key, normalize_job_title_key(job.get("title"))), []).append(job)
    return index, analyses


def _reconcile(job: Mapping[str, object], index: Mapping, analyses: Mapping[str, Mapping]) -> dict[str, object]:
    company = str(job.get("company") or job.get("company_name") or "")
    matches = list(index.get((normalize_company_name(company), normalize_job_title_key(job.get("title"))), []))
    if not matches:
        action = "insert_new"
    elif len(matches) > 1:
        action = "conflict_multiple_existing"
    else:
        existing = matches[0]
        analysis = analyses.get(str(existing.get("id") or ""))
        action = "reuse_existing_scored" if analysis and analysis.get("match_score") is not None else "update_existing"
    return {
        "company": company,
        "title": job.get("title"),
        "candidate_company_id": job.get("company_id"),
        "action": action,
        "existing_job_ids": [row.get("id") for row in matches],
    }


def build_preview(
    replay: Path,
    company_acceptance: Path,
    retry_dirs: Iterable[Path],
    catalog: Mapping[str, object],
) -> tuple[dict[str, list[dict]], dict[str, object]]:
    companies = [
        dict(row)
        for row in _jsonl(company_acceptance / "company-status.jsonl")
        if str(row.get("status") or "") != "unusable"
        and str(row.get("list_status") or "") != "unusable"
    ]
    admitted_company_ids = {
        str(row.get("company_id") or "") for row in companies
    }
    retries = _retry_results(retry_dirs)
    original = {
        name: {_key(row): row for row in _jsonl(replay / f"{name}.jsonl")}
        for name in ("new-failures", "missing-jd", "existing-repairs")
    }
    ready = [
        proposed
        for row in _jsonl(replay / "successful-jd-reuse.jsonl")
        for proposed in [dict(row.get("proposed_job") or {})]
        if str(proposed.get("company_id") or "") in admitted_company_ids
    ]
    failed = []
    repairs = []
    for bucket, records in original.items():
        for key, row in records.items():
            if str(row.get("company_id") or "") not in admitted_company_ids:
                continue
            retry = retries.get(key)
            if retry is not None and retry.get("success") is True:
                base_name = "repair_target_job" if bucket == "existing-repairs" else "placeholder" if bucket == "new-failures" else "pending_job"
                completed = _completed_job(row.get(base_name) or {}, retry)
                (repairs if bucket == "existing-repairs" else ready).append(completed)
            elif bucket != "existing-repairs":
                base_name = "placeholder" if bucket == "new-failures" else "pending_job"
                placeholder = dict(row.get(base_name) or {})
                if retry is not None:
                    placeholder.update(
                        capture_status="failed",
                        capture_failure_reason=str(
                            retry.get("failure_reason") or retry.get("status") or "capture_failed"
                        ),
                        acceptance_evidence=retry.get("acceptance_evidence"),
                    )
                failed.append(placeholder)

    index, analyses = _catalog_index(catalog)
    reconciliation = [_reconcile(job, index, analyses) for job in ready]
    files = {
        "jobs-ready": ready,
        "jobs-failed": failed,
        "existing-repairs-ready": repairs,
        "companies": companies,
        "reconciliation": reconciliation,
    }
    report = {
        "schema": "title-first-import-preview.v1",
        "read_only": True,
        "formal_database_writes": 0,
        "model_calls": 0,
        "counts": {name: len(rows) for name, rows in files.items()},
        "reconciliation_actions": dict(Counter(row["action"] for row in reconciliation)),
        "company_statuses": dict(Counter(str(row.get("status")) for row in companies)),
        "application_records_changed": 0,
    }
    return files, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--company-acceptance", required=True, type=Path)
    parser.add_argument("--retry-dir", action="append", default=[], type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite an existing preview")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    files, report = build_preview(args.replay, args.company_acceptance, args.retry_dir, catalog)
    args.output.mkdir(parents=True)
    for name, rows in files.items():
        with (args.output / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
