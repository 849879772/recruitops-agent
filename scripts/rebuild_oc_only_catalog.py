"""Rebuild the Agent company catalog exclusively from the latest OC snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.discovery import (
    SourceLead,
    consolidate_source_leads,
    filter_oc_snapshot,
    normalize_company_name,
    normalize_oc_destination_url,
    reconcile_companies,
)
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, CompanySnapshot, JobSnapshot
from packages.tools.oc_candidates import infer_candidate_crawler
from scripts.oc_catalog_evaluation import (
    EvaluationIndex,
    canonical_evaluation_url,
    evaluation_recency,
    evaluation_urls,
    load_catalog_evaluations,
)


def _read_company_rows(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("companies") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("companies config must contain a list of mappings")
    return [dict(row) for row in rows]


def _stable_oc_id(lead: SourceLead) -> str:
    identity = f"{normalize_company_name(lead.canonical_name)}\0{lead.source_identity or ''}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"oc-{digest}"


def _organization_id(name: object) -> str:
    normalized = normalize_company_name(name)
    return "org-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _deduplicated_strings(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _source_coverage(
    leads: list[SourceLead],
    evaluations: EvaluationIndex,
    prior: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    prior_by_source = {
        (normalize_company_name(item.get("project_name")),
         normalize_oc_destination_url(item.get("source_url"))): item
        for item in prior if isinstance(item, Mapping)
    }
    coverage = []
    for lead in leads:
        for url in lead.source_urls or ("",):
            evaluation = evaluations.for_source(lead, url)
            previous = prior_by_source.get((normalize_company_name(lead.canonical_name), url))
            preserved = False
            if previous and previous.get("evidence_status") != "no_evidence":
                if evaluation is None or evaluation_recency(previous) > evaluation_recency(evaluation):
                    evaluation = previous
                    preserved = True
            item: dict[str, Any] = {
                "project_name": lead.canonical_name,
                "source_url": url,
                "canonical_url": canonical_evaluation_url(evaluation or {}) or url,
                "source_urls": _deduplicated_strings([url, *evaluation_urls(evaluation or {})]),
                "integration_status": str((evaluation or {}).get("integration_status") or "unverified"),
                "evidence_status": "no_evidence",
            }
            if evaluation is not None:
                item["evidence_status"] = (
                    "accepted" if evaluation.get("integration_status") == "connected_complete"
                    else "known_failure"
                )
                item["preserved"] = preserved
                for field in (
                    "lead_key", "crawler_key", "completed_at", "evaluation_file", "error_code",
                    "accepted_count", "raw_job_count", "complete_jd_count", "incomplete_jd_count",
                    "pagination_complete", "completeness_known",
                ):
                    if field in evaluation:
                        item[field] = evaluation[field]
            coverage.append(item)
    return coverage


def _apply_source_coverage(
    row: dict[str, Any], leads: list[SourceLead], evaluations: EvaluationIndex,
) -> None:
    coverage = _source_coverage(leads, evaluations, row.get("oc_source_coverage") or [])
    accepted = [item for item in coverage if item["evidence_status"] == "accepted"]
    failures = [item for item in coverage if item["evidence_status"] == "known_failure"]
    row["oc_source_coverage"] = coverage
    row["oc_canonical_urls"] = _deduplicated_strings(item["canonical_url"] for item in coverage)
    row["oc_all_sources_complete"] = bool(coverage) and len(accepted) == len(coverage)
    row["oc_coverage_status"] = (
        "complete" if row["oc_all_sources_complete"] else
        "partial" if accepted else "failed" if failures else "no_evidence"
    )
    selected = max(accepted or failures, key=evaluation_recency, default=None)
    current_url = str(row.get("careers_url") or "")
    if accepted:
        row["careers_url"] = selected["canonical_url"] or current_url
        # Acceptance updates the adapter even when the URL has not changed.
        if selected.get("crawler_key"):
            row["crawler"] = str(selected["crawler_key"])
    elif not current_url:
        row["careers_url"] = (selected or coverage[0])["canonical_url"]
    if not row.get("crawler"):
        row["crawler"] = str((selected or {}).get("crawler_key") or "") or (
            infer_candidate_crawler(row.get("careers_url") or "") or ""
        )
    # connected denotes a usable primary entry; completeness is source-scoped.
    _apply_evaluation_status(row, selected, default_status="not_connected")


def _apply_evaluation_status(
    row: dict[str, Any],
    evaluation: Mapping[str, Any] | None,
    *,
    default_status: str,
) -> None:
    if evaluation is None:
        row["integration_status"] = default_status
        if default_status != "connected":
            row["integration_note"] = (
                "OC-only catalog: no_complete_crawler_acceptance_evidence"
            )
        return

    is_complete = evaluation.get("integration_status") == "connected_complete"
    row["integration_status"] = "connected" if is_complete else "not_connected"
    if is_complete:
        row.pop("integration_note", None)
        return

    reason = (
        evaluation.get("error_code")
        or evaluation.get("integration_status")
        or "no_complete_crawler_acceptance_evidence"
    )
    row["integration_note"] = f"OC-only catalog: {reason}"


def _merge_oc_evidence(row: dict[str, Any], leads: list[SourceLead], captured_at: str | None) -> dict[str, Any]:
    row.update(
        {
            "discovery_source": "oc_snapshot",
            "oc_source_urls": _deduplicated_strings(
                url for lead in leads for url in lead.source_urls
            ),
            "oc_source_projects": _deduplicated_strings(
                project
                for lead in leads
                for project in lead.metadata.get("source_project_names", [lead.canonical_name])
            ),
            "oc_snapshot_captured_at": captured_at,
            "industries": _deduplicated_strings(
                item for lead in leads for item in lead.metadata.get("industries", [])
            ),
            "recruitment_types": _deduplicated_strings(
                item for lead in leads for item in lead.metadata.get("recruitment_types", [])
            ),
            "recruitment_targets": _deduplicated_strings(
                item for lead in leads for item in lead.metadata.get("recruitment_targets", [])
            ),
        }
    )
    row.setdefault("organization_id", _organization_id(row.get("name")))
    row.setdefault("recruitment_unit_id", str(row.get("id") or ""))
    row.setdefault("recruitment_unit_name", str(row.get("name") or ""))
    identities = _deduplicated_strings(lead.source_identity for lead in leads)
    if len(identities) == 1:
        row.setdefault("source_identity", identities[0])
    return row


def _catalog_lead_groups(
    raw_leads: list[SourceLead], current_rows: list[dict[str, Any]],
) -> list[tuple[str | None, list[SourceLead]]]:
    """Build company rows without collapsing distinct businesses on one ATS tenant.

    A platform tenant is a crawler reuse boundary, not proof that every project
    belongs to one company. Existing names/aliases are the strongest business
    identity; an unmatched project remains standalone until a parent-company
    relationship is evidenced by the current catalog.
    """
    matched_global: dict[str, list[SourceLead]] = defaultdict(list)
    standalone: list[SourceLead] = []
    # Omit source identities so shared tenants neither establish a parent nor
    # veto an explicit name/alias/project-parent match. Visit URL-less leads too.
    named_rows = [
        {field: row[field] for field in ("id", "name", "aliases") if field in row}
        for row in current_rows
    ]
    reconciliation = reconcile_companies(raw_leads, named_rows)
    for lead in reconciliation.existing:
        matched_global[normalize_company_name(lead.matched_company)].append(lead)
    standalone.extend(reconciliation.new)
    standalone.extend(reconciliation.ambiguous)
    return [
        *[(key, members) for key, members in matched_global.items()],
        *((None, [lead]) for lead in standalone),
    ]


def build_oc_only_catalog(
    snapshot_path: Path,
    current_rows: list[dict[str, Any]],
    evaluation_path: Path | None = None,
    *,
    evaluation_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preview OC-backed units with source-scoped acceptance; never write state."""

    source = filter_oc_snapshot(snapshot_path)
    raw_leads = list(source.leads)
    catalog_groups = _catalog_lead_groups(raw_leads, current_rows)
    selection = load_catalog_evaluations(
        source, evaluation_path,
        evaluation_dir=evaluation_dir or snapshot_path.parent.parent / "evals",
        snapshot_path=snapshot_path,
    )
    rows: list[dict[str, Any]] = []
    current_by_name = {
        normalize_company_name(row.get("name")): row for row in current_rows
    }
    for matched_name, group in catalog_groups:
        if matched_name:
            existing = dict(current_by_name[matched_name])
            _apply_source_coverage(existing, group, selection.index)
            rows.append(_merge_oc_evidence(existing, group, source.captured_at))
        else:
            for lead in group:
                existing = {"id": _stable_oc_id(lead), "name": lead.canonical_name}
                _apply_source_coverage(existing, [lead], selection.index)
                rows.append(_merge_oc_evidence(existing, [lead], source.captured_at))

    rows.sort(key=lambda row: normalize_company_name(row.get("name")))
    ids = [str(row["id"]) for row in rows]
    names = [normalize_company_name(row["name"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("OC-only catalog contains duplicate company ids")
    if len(names) != len(set(names)):
        raise ValueError("OC-only catalog contains duplicate company names")
    if any(row.get("discovery_source") != "oc_snapshot" for row in rows):
        raise ValueError("OC-only catalog contains a non-OC company")

    summary = {
        "snapshot_rows": source.rows_seen,
        "accepted_rows": int(source.accepted_rows or 0),
        "consolidated_entries": len(consolidate_source_leads(raw_leads)),
        "companies": len(rows),
        "connected": sum(row.get("integration_status") == "connected" for row in rows),
        "not_connected": sum(row.get("integration_status") != "connected" for row in rows),
        "preserved_existing": sum(bool(matched_name) for matched_name, _ in catalog_groups),
        "new_or_ambiguous": sum(not matched_name for matched_name, _ in catalog_groups),
        "complete_source_coverage": sum(row["oc_all_sources_complete"] for row in rows),
        "partial_source_coverage": sum(row["oc_coverage_status"] == "partial" for row in rows),
        "evaluation": selection.summary(),
    }
    return rows, summary


def write_catalog(path: Path, rows: list[dict[str, Any]]) -> None:
    rendered = yaml.safe_dump(
        {"companies": rows}, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    _read_company_rows(temporary)
    temporary.replace(path)


def prune_database(
    database_url: str,
    rows: list[dict[str, Any]],
    *,
    replace_catalog: bool = False,
) -> dict[str, int]:
    """Replace or prune catalog data without modifying application records."""

    retained_company_ids = {str(row["id"]) for row in rows}
    storage = Storage.from_url(database_url)
    with storage.transaction(write=True) as session:
        applications_before = (
            session.scalar(select(func.count()).select_from(ApplicationSnapshot)) or 0
        )
        protected_job_ids = {
            value
            for value in session.scalars(
                select(ApplicationSnapshot.job_id).where(ApplicationSnapshot.job_id.is_not(None))
            )
            if value
        }
        before_companies = session.scalar(select(func.count()).select_from(CompanySnapshot)) or 0
        before_jobs = session.scalar(select(func.count()).select_from(JobSnapshot)) or 0
        if replace_catalog:
            deleted_jobs = session.execute(delete(JobSnapshot)).rowcount or 0
            deleted_companies = session.execute(delete(CompanySnapshot)).rowcount or 0
        else:
            job_filter = JobSnapshot.company_id.not_in(retained_company_ids)
            if protected_job_ids:
                job_filter = job_filter & JobSnapshot.id.not_in(protected_job_ids)
            deleted_jobs = session.execute(delete(JobSnapshot).where(job_filter)).rowcount or 0
            deleted_companies = session.execute(
                delete(CompanySnapshot).where(CompanySnapshot.id.not_in(retained_company_ids))
            ).rowcount or 0

        now = datetime.now(timezone.utc)
        for row in rows:
            company_id = str(row["id"])
            model = session.get(CompanySnapshot, company_id)
            if model is None:
                model = CompanySnapshot(id=company_id, created_at=now, source="oc_snapshot")
                session.add(model)
            model.name = str(row["name"])
            model.aliases = list(row.get("aliases") or [])
            model.campus_url = str(row.get("careers_url") or "") or None
            model.crawler_key = str(row.get("crawler") or "") or None
            model.integration_status = str(row.get("integration_status") or "not_connected")
            model.updated_at = now
            model.source = "oc_snapshot"
            model.source_ref = f"givemeoc_latest.json:{company_id}"
        session.flush()
        after_companies = session.scalar(select(func.count()).select_from(CompanySnapshot)) or 0
        after_jobs = session.scalar(select(func.count()).select_from(JobSnapshot)) or 0
        applications_after = (
            session.scalar(select(func.count()).select_from(ApplicationSnapshot)) or 0
        )

    return {
        "companies_before": int(before_companies),
        "companies_after": int(after_companies),
        "companies_deleted": int(deleted_companies),
        "jobs_before": int(before_jobs),
        "jobs_after": int(after_jobs),
        "jobs_deleted": int(deleted_jobs),
        "applications_before": int(applications_before),
        "applications_after": int(applications_after),
        "application_jobs_preserved": 0 if replace_catalog else len(protected_job_ids),
        "application_job_refs_retained": len(protected_job_ids),
    }


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=settings.oc_snapshot_file)
    parser.add_argument("--companies", type=Path, default=settings.companies_config)
    parser.add_argument(
        "--evaluation",
        type=Path,
        help="Use one explicit evaluation (including legacy results-only artifacts).",
    )
    parser.add_argument(
        "--evaluation-dir", type=Path, default=settings.agent_root / ".data/evals",
        help="Select the latest compatible full evaluation and newer partial overlays.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--prune-database", action="store_true")
    parser.add_argument(
        "--replace-catalog",
        action="store_true",
        help=(
            "Delete every existing company, job, and cascading job analysis before writing "
            "the OC-only company catalog. Application rows are not modified."
        ),
    )
    args = parser.parse_args()
    if args.snapshot is None:
        raise ValueError("OC snapshot path is required")
    rows, summary = build_oc_only_catalog(
        args.snapshot,
        _read_company_rows(args.companies),
        args.evaluation,
        evaluation_dir=args.evaluation_dir,
    )
    result: dict[str, Any] = {"catalog": summary, "applied": bool(args.apply)}
    if args.prune_database and not args.apply:
        parser.error("--prune-database requires --apply")
    if args.replace_catalog and not args.prune_database:
        parser.error("--replace-catalog requires --prune-database")
    if args.apply:
        write_catalog(args.companies, rows)
        if args.prune_database:
            result["database"] = prune_database(
                settings.database_url,
                rows,
                replace_catalog=args.replace_catalog,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
