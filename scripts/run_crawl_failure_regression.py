"""Bounded, read-only regression through the production crawl/worker/pipeline path."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from threading import Lock

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.pipeline.daily import DailyRecruitmentPipeline
from packages.pipeline.isolation import (
    crawl_company_result_isolated,
    fetch_job_detail_result_isolated,
)


def summarize_results(results: list[dict]) -> dict:
    complete_lists = [row for row in results if (
        row.get("crawl_evidence", {}).get("completeness_known") is True
        and row["crawl_evidence"].get("pagination_complete") is True
        and not row["crawl_evidence"].get("has_more")
        and not row.get("failure_reason")
    )]
    details = [detail for row in results for detail in row.get("jd_results", [])]
    attempted = [detail for detail in details if detail.get("status") != "not_sampled"]
    return {
        "companies_tested": len(results),
        "complete_crawls_with_jobs": sum(row.get("raw_job_count", 0) > 0 for row in complete_lists),
        "verified_empty_activities": sum(
            row.get("raw_job_count", 0) == 0 and row.get("run_reason") == "activity_empty"
            for row in complete_lists
        ),
        "failed_companies": sum(bool(row.get("failure_reason")) for row in results),
        "jd_attempted": len(attempted),
        "jd_completed": sum(detail.get("status") == "complete" for detail in attempted),
        "jd_not_sampled": len(details) - len(attempted),
        "jd_validation": "exercised" if attempted else "not_exercised",
    }


def select_companies(rows: list[dict], names: list[str]) -> list[dict]:
    selected = []
    for name in dict.fromkeys(names):
        matches = [r for r in rows if name in [r.get("name"), *(r.get("aliases") or [])]]
        if len(matches) != 1:
            raise ValueError(f"Company must resolve uniquely in the catalog: {name}")
        row = dict(matches[0])
        if row.get("discovery_source") != "oc_snapshot":
            raise ValueError(f"Company is not backed by the OC snapshot: {name}")
        row["integration_status"] = "connected"  # Select the failure for this dry run only.
        if not any(r["id"] == row["id"] for r in selected):
            selected.append(row)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", action="append", required=True)
    parser.add_argument("--companies", type=Path, default=ROOT / "config/companies.yaml")
    parser.add_argument("--baseline", type=Path, default=ROOT / ".data/evals/oc_feishu_timeout_final.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--timeout-seconds", type=int, choices=range(15, 301), default=120)
    parser.add_argument("--max-details", type=int, choices=range(0, 21), default=0)
    parser.add_argument("--replay", type=Path, help="Reuse a prior raw.json instead of refetching lists.")
    args = parser.parse_args()
    catalog = yaml.safe_load(args.companies.read_text(encoding="utf-8"))["companies"]
    companies = select_companies(catalog, args.company)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    prior = baseline.get("results", [])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists():
        raise ValueError("Use a new output directory to preserve previous evidence.")
    captured = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else {}
    lock = Lock()
    detail_calls: dict[str, int] = {}
    started = datetime.now(timezone.utc).isoformat()

    def crawl(company):
        if args.replay:
            return captured[company.id]
        result = crawl_company_result_isolated(
            company.crawler_config(), timeout_seconds=args.timeout_seconds,
        )
        with lock:
            captured[company.id] = result
            (output / "raw.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def hydrate(job):
        company_id = job["company_id"]
        with lock:
            count = detail_calls.get(company_id, 0)
            if count >= args.max_details:
                return {"detail": "", "status": "not_sampled", "source": "regression_budget"}
            detail_calls[company_id] = count + 1
        return fetch_job_detail_result_isolated(job, timeout_seconds=min(60, args.timeout_seconds))

    def check(company):
        with tempfile.TemporaryDirectory(prefix="recruitops-regression-") as directory:
            path = Path(directory) / "companies.yaml"
            path.write_text(yaml.safe_dump({"companies": [company]}, allow_unicode=True), encoding="utf-8")
            pipeline = DailyRecruitmentPipeline(
                companies_path=path, crawler=crawl, jd_hydrator=hydrate if args.max_details else None,
                max_concurrency=1, match_max_concurrency=1,
            ).run(dry_run=True)
        row = pipeline.company_results[0].to_dict()
        old = [r for r in prior if r.get("company") in [company["name"], *(company.get("aliases") or [])]]
        row["previous_results"] = [{k: r.get(k) for k in (
            "lead_key", "company", "source_url", "integration_status", "error_code", "raw_job_count", "incomplete_jd_count",
        )} for r in old]
        row["written"] = pipeline.written
        print(json.dumps({k: row[k] for k in ("company_name", "status", "failure_reason", "raw_job_count", "filtered_reasons")}, ensure_ascii=False), flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(check, companies))
    report = {
        "scope": "targeted_pipeline", "read_only": True, "model_calls": 0, "database_writes": 0,
        "started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(),
        "baseline": str(args.baseline), "catalog_sha256": hashlib.sha256(args.companies.read_bytes()).hexdigest(),
        "max_details_per_company": args.max_details, "details_attempted": detail_calls,
        "replayed": bool(args.replay), "summary": summarize_results(results), "results": results,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output / 'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
