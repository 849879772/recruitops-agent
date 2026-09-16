"""Backfill location-independent job identities and merge proven duplicates."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, select, text, update

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.domain.job_identity import build_job_identity
from packages.storage import Storage
from packages.storage.models import (
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
)


def _companies(path: Path) -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        str(row["id"]): dict(row)
        for row in payload.get("companies", [])
        if isinstance(row, dict) and row.get("id")
    }


def migrate(database_url: str, companies_path: Path, *, apply: bool) -> dict[str, int]:
    companies = _companies(companies_path)
    storage = Storage.from_url(database_url)
    with storage.session() as session:
        jobs = list(session.scalars(select(JobSnapshot)))
        application_job_ids = {
            value for value in session.scalars(select(ApplicationSnapshot.job_id)) if value
        }

    groups: dict[str, list[tuple[JobSnapshot, Any]]] = defaultdict(list)
    for job in jobs:
        company = companies.get(job.company_id, {"id": job.company_id})
        identity = build_job_identity(
            company,
            {
                "id": job.native_job_id or job.id,
                "title": job.title,
                "detail_url": job.detail_url,
            },
        )
        groups[identity.business_key].append((job, identity))

    duplicate_groups = [items for items in groups.values() if len(items) > 1]
    result = {
        "jobs": len(jobs),
        "identity_groups": len(groups),
        "duplicate_groups": len(duplicate_groups),
        "duplicates_to_merge": sum(len(items) - 1 for items in duplicate_groups),
        "application_links_repointed": 0,
        "applied": int(apply),
    }
    if not apply:
        return result

    with storage.write_transaction() as session:
        for business_key, items in groups.items():
            ordered = sorted(
                items,
                key=lambda item: (
                    0 if item[0].id in application_job_ids else 1,
                    item[0].created_at,
                    item[0].id,
                ),
            )
            canonical_job, canonical_identity = ordered[0]
            canonical = session.get(JobSnapshot, canonical_job.id)
            if canonical is None:
                continue
            for duplicate_job, _identity in ordered[1:]:
                duplicate = session.get(JobSnapshot, duplicate_job.id)
                if duplicate is None:
                    continue
                linked = session.execute(
                    update(ApplicationSnapshot)
                    .where(ApplicationSnapshot.job_id == duplicate.id)
                    .values(job_id=canonical.id)
                ).rowcount or 0
                result["application_links_repointed"] += int(linked)
                canonical_analysis = session.get(JobAnalysisSnapshot, canonical.id)
                duplicate_analysis = session.get(JobAnalysisSnapshot, duplicate.id)
                if canonical_analysis is None and duplicate_analysis is not None:
                    duplicate_analysis.job_id = canonical.id
                    session.flush()
                elif duplicate_analysis is not None:
                    session.delete(duplicate_analysis)
                session.delete(duplicate)

            company = companies.get(canonical.company_id, {"id": canonical.company_id})
            canonical.organization_id = str(
                company.get("organization_id") or canonical.company_id
            )
            canonical.recruitment_unit_id = str(
                company.get("recruitment_unit_id") or canonical.company_id
            )
            canonical.recruitment_campaign_id = str(
                company.get("recruitment_campaign_id") or ""
            ) or None
            canonical.source_platform = str(company.get("crawler") or "") or None
            canonical.source_tenant = str(company.get("source_identity") or "") or None
            canonical.native_job_id = canonical_identity.native_job_id
            canonical.normalized_detail_url = canonical_identity.normalized_detail_url
            canonical.business_key = business_key

        for company_id, company in companies.items():
            snapshot = session.get(CompanySnapshot, company_id)
            if snapshot is None:
                continue
            snapshot.organization_id = str(company.get("organization_id") or company_id)
            snapshot.recruitment_unit_name = str(
                company.get("recruitment_unit_name") or company.get("name") or ""
            ) or None
            snapshot.source_identity = str(company.get("source_identity") or "") or None
        session.flush()
        session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_job_snapshots_business_key "
                "ON job_snapshots (business_key) WHERE business_key IS NOT NULL"
            )
        )
    return result


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(
                settings.database_url,
                settings.companies_config,
                apply=args.apply,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
