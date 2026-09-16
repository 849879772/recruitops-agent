"""Run the standalone crawler kernel without importing the legacy project.

Use ``python -m scripts.run_agent_crawler`` from the RecruitOps-Agent root.
The ``--company`` value may be a JSON object, a JSON file path, or a company
name paired with ``--crawler`` and ``--careers-url``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from packages.recruitment_core import CompanyConfig, crawl_company_with_evidence


def _company_value(value: str, args: argparse.Namespace) -> dict[str, Any]:
    candidate = value.strip()
    if candidate.startswith("{"):
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise ValueError("company JSON must be an object")
        return payload

    path = Path(candidate)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("company JSON file must contain an object")
        return payload

    if not args.crawler or not args.careers_url:
        raise ValueError("a company name requires --crawler and --careers-url")
    return {
        "name": candidate,
        "crawler": args.crawler,
        "careers_url": args.careers_url,
        "campaign_urls": args.campaign_url,
        "link_kind": args.link_kind,
        "campaign_text": args.campaign_text,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one independent recruitment crawler")
    parser.add_argument("--company", required=True, help="company name, JSON object, or JSON file")
    parser.add_argument("--crawler", help="crawler key when --company is a name")
    parser.add_argument("--careers-url", help="careers URL when --company is a name")
    parser.add_argument("--campaign-url", action="append", default=[])
    parser.add_argument("--link-kind", default="")
    parser.add_argument("--campaign-text", default="")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    company = CompanyConfig.from_legacy(_company_value(args.company, args))
    result = crawl_company_with_evidence(company)
    jobs = result["jobs"]
    print(json.dumps(
        {
            "company": company.name,
            "crawler_key": company.crawler,
            "configured_urls": [company.careers_url, *company.campaign_urls],
            "source_url": company.careers_url,
            "jobs": jobs,
            "raw_job_count": len(jobs),
            "pagination_complete": result["pagination_complete"],
            "completeness_known": result["completeness_known"],
            "pages_seen": result["pages_seen"],
            "total_pages": result["total_pages"],
            "has_more": result["has_more"],
            "advertised_total": result["advertised_total"],
            "termination_reasons": result["termination_reasons"],
            "source_runs": result["source_runs"],
            "effective_source_urls": result["effective_source_urls"],
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
