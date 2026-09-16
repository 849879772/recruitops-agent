"""Import an approved title-first preview into the formal catalog once."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.company_registry import CompanySourceRecord
from packages.discovery.reconciliation import normalize_company_name
from packages.domain.job_identity import build_job_identity
from packages.matching.title_policy import normalize_job_title_key
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, CompanySnapshot, JobSnapshot, utc_now


SOURCE = "offerbiu.title_first"
SOURCE_URL = "https://offerbiu.com/companies/"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"{path.name}:{line_number} is not an object")
            rows.append(dict(value))
    return rows


def _verified_backup(path: Path, expected_sha256: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError("verified non-empty PostgreSQL backup required")
    digest = sha256(resolved.read_bytes()).hexdigest()
    if digest.casefold() != expected_sha256.strip().casefold():
        raise ValueError("PostgreSQL backup checksum mismatch")
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": digest}


def _application_digest(rows: Iterable[ApplicationSnapshot]) -> str:
    payload = []
    for row in sorted(rows, key=lambda item: item.id):
        payload.append({
            column.name: _jsonable(getattr(row, column.name))
            for column in ApplicationSnapshot.__table__.columns
        })
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _first_url(company: Mapping[str, Any]) -> str:
    for source in company.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        value = str(source.get("source_url") or "").strip()
        if urlsplit(value).scheme in {"http", "https"}:
            return value
    return ""


def _source_status(source: Mapping[str, Any], company: Mapping[str, Any]) -> str:
    capture = source.get("capture") if isinstance(source.get("capture"), Mapping) else {}
    status = str(capture.get("status") or company.get("list_status") or "failed").strip()
    return status if status in {"pending", "running", "complete", "partial", "failed"} else "failed"


def _parse_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        if not raw:
            return fallback
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return fallback
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _job_snapshot(row: Mapping[str, Any], now: datetime) -> JobSnapshot:
    company_id = str(row.get("company_id") or "").strip()
    title = str(row.get("title") or "").strip()
    detail_url = str(row.get("detail_url") or row.get("jd_url") or "").strip()
    if not company_id or not title or not detail_url:
        raise ValueError("job requires company_id, title and detail_url")
    identity = build_job_identity(
        {"id": company_id, "organization_id": company_id, "recruitment_unit_id": company_id},
        {**row, "detail_url": detail_url},
    )
    status = str(row.get("capture_status") or "failed").strip()
    if status not in {"complete", "failed"}:
        raise ValueError(f"unsupported import capture status: {status}")
    jd_raw = str(row.get("jd_raw") or "").strip() or None
    if status == "complete" and not jd_raw:
        raise ValueError("complete job requires non-empty official JD")
    evidence = dict(row.get("capture_evidence") or {}) if isinstance(row.get("capture_evidence"), Mapping) else {}
    acceptance = str(row.get("acceptance_evidence") or "").strip()
    if acceptance:
        evidence.setdefault("acceptance_evidence", acceptance)
    seen_at = _parse_time(row.get("published_at") or row.get("last_seen_at"), now)
    return JobSnapshot(
        id=identity.stable_id,
        company_id=company_id,
        title=title,
        city=str(row.get("city") or "").strip() or None,
        detail_url=detail_url,
        jd_raw=jd_raw,
        cohort=2027,
        cohort_status="confirmed",
        batch="formal",
        match_score=None,
        first_seen_at=seen_at,
        last_seen_at=now,
        organization_id=company_id,
        recruitment_unit_id=company_id,
        recruitment_campaign_id=str(row.get("campaign_url") or "").strip() or None,
        source_platform=str(row.get("source_platform") or "offerbiu").strip(),
        source_tenant=urlsplit(detail_url).hostname,
        native_job_id=str(row.get("native_job_id") or row.get("id") or "").strip() or None,
        normalized_detail_url=identity.normalized_detail_url,
        business_key=identity.business_key,
        capture_status=status,
        capture_failure_reason=str(row.get("capture_failure_reason") or "").strip(),
        availability_status="active",
        title_key=normalize_job_title_key(title),
        capture_evidence=evidence,
        created_at=now,
        updated_at=now,
        source=SOURCE,
        source_ref=f"{SOURCE}:job:{company_id}:{normalize_job_title_key(title)}",
    )


def _load_preview(preview: Path) -> dict[str, list[dict[str, Any]]]:
    companies = [
        row for row in _jsonl(preview / "companies.jsonl")
        if str(row.get("status") or "") != "unusable"
        and str(row.get("list_status") or "") != "unusable"
    ]
    company_ids = {str(row.get("company_id") or "") for row in companies}
    files = {
        "companies": companies,
        "jobs_ready": _jsonl(preview / "jobs-ready.jsonl"),
        "jobs_failed": _jsonl(preview / "jobs-failed.jsonl"),
        "repairs": _jsonl(preview / "existing-repairs-ready.jsonl"),
    }
    for name in ("jobs_ready", "jobs_failed", "repairs"):
        invalid = [row for row in files[name] if str(row.get("company_id") or "") not in company_ids]
        if invalid:
            raise ValueError(f"{name} contains jobs for excluded companies")
    return files


def import_preview(
    storage: Storage,
    preview: Path,
    *,
    backup: Path,
    backup_sha256: str,
    apply: bool = False,
) -> dict[str, Any]:
    backup_receipt = _verified_backup(backup, backup_sha256)
    files = _load_preview(preview.resolve())
    now = utc_now()

    with storage.session() as session:
        existing_companies = {row.id: row for row in session.scalars(select(CompanySnapshot))}
        existing_jobs = list(session.scalars(select(JobSnapshot)))
        applications = list(session.scalars(select(ApplicationSnapshot)))
        application_digest_before = _application_digest(applications)

    company_names = {
        row.id: normalize_company_name(row.name) for row in existing_companies.values()
    }
    existing_title_keys = {
        (company_names.get(row.company_id, ""), normalize_job_title_key(row.title)): row.id
        for row in existing_jobs
    }
    existing_business_keys = {row.business_key for row in existing_jobs if row.business_key}
    new_companies: list[CompanySnapshot] = []
    for row in files["companies"]:
        company_id = str(row.get("company_id") or "").strip()
        if not company_id or company_id in existing_companies:
            continue
        name = str(row.get("company_name") or company_id).strip()
        new_companies.append(CompanySnapshot(
            id=company_id,
            name=name,
            aliases=[],
            campus_url=_first_url(row) or None,
            crawler_key="offerbiu",
            integration_status="crawl_failed" if row.get("status") == "failed" else "connected",
            organization_id=company_id,
            recruitment_unit_name=name,
            source_identity=f"offerbiu:{company_id}",
            created_at=now,
            updated_at=now,
            source=SOURCE,
            source_ref=f"{SOURCE}:company:{company_id}",
        ))
        company_names[company_id] = normalize_company_name(name)

    new_jobs: list[JobSnapshot] = []
    skipped_existing = 0
    skipped_business_key = 0
    for raw in [*files["jobs_ready"], *files["jobs_failed"]]:
        job = _job_snapshot(raw, now)
        live_key = (company_names.get(job.company_id, ""), normalize_job_title_key(job.title))
        if live_key in existing_title_keys:
            skipped_existing += 1
            continue
        if job.business_key in existing_business_keys:
            skipped_business_key += 1
            continue
        existing_title_keys[live_key] = job.id
        existing_business_keys.add(job.business_key)
        new_jobs.append(job)

    repairs: list[tuple[JobSnapshot, Mapping[str, Any]]] = []
    existing_by_id = {row.id: row for row in existing_jobs}
    for raw in files["repairs"]:
        job_id = str(raw.get("id") or "").strip()
        target = existing_by_id.get(job_id)
        if target is None:
            raise ValueError(f"repair target is missing: {job_id}")
        if str(raw.get("capture_status") or "") != "complete" or not str(raw.get("jd_raw") or "").strip():
            raise ValueError(f"repair target is not a complete capture: {job_id}")
        repairs.append((target, raw))

    source_rows: list[CompanySourceRecord] = []
    for company in files["companies"]:
        company_id = str(company.get("company_id") or "").strip()
        for source in company.get("sources") or []:
            if not isinstance(source, Mapping):
                continue
            entry_url = str(source.get("source_url") or "").strip()
            if not entry_url:
                continue
            source_key = str(source.get("source_key") or sha256(entry_url.encode()).hexdigest()).strip()
            status = _source_status(source, company)
            record_id = sha256(f"offerbiu\0{source_key}".encode()).hexdigest()
            capture = source.get("capture") if isinstance(source.get("capture"), Mapping) else {}
            source_rows.append(CompanySourceRecord(
                id=record_id,
                source="offerbiu",
                source_record_id=source_key,
                company_name=str(company.get("company_name") or company_id),
                company_id=company_id,
                source_url=SOURCE_URL,
                entry_url=entry_url,
                original_entry_url=entry_url,
                final_url=str(capture.get("final_url") or entry_url),
                status=status,
                failure_stage="list" if status in {"partial", "failed"} else "",
                reason_code=str(capture.get("reason_code") or ""),
                reason=str(capture.get("reason") or ""),
                job_count=int(capture.get("raw_job_count") or company.get("admitted_titles") or 0),
                jd_pending_count=int(company.get("unresolved_detail_failure_count") or company.get("unresolved_detail_pending_count") or 0),
                last_success_job_count=int(capture.get("raw_job_count") or company.get("admitted_titles") or 0) if status in {"complete", "partial"} else 0,
                pagination_complete=capture.get("pagination_complete"),
                last_attempt_at=now,
                created_at=now,
                updated_at=now,
            ))

    report: dict[str, Any] = {
        "schema": "title-first-formal-import.v1",
        "preview": str(preview.resolve()),
        "backup": backup_receipt,
        "apply": apply,
        "input": {
            "companies": len(files["companies"]),
            "jobs_ready": len(files["jobs_ready"]),
            "jobs_failed": len(files["jobs_failed"]),
            "repairs": len(files["repairs"]),
        },
        "planned": {
            "new_companies": len(new_companies),
            "new_jobs": len(new_jobs),
            "new_complete_jobs": sum(row.capture_status == "complete" for row in new_jobs),
            "new_failed_jobs": sum(row.capture_status == "failed" for row in new_jobs),
            "repairs": len(repairs),
            "source_records": len(source_rows),
            "skipped_existing_company_title": skipped_existing,
            "skipped_existing_business_key": skipped_business_key,
        },
        "applications": {
            "before_count": len(applications),
            "before_sha256": application_digest_before,
        },
    }
    if not apply:
        report["written"] = False
        return report

    with storage.write_transaction() as session:
        live_applications = list(session.scalars(select(ApplicationSnapshot)))
        if len(live_applications) != len(applications) or _application_digest(live_applications) != application_digest_before:
            raise RuntimeError("application records changed after import planning")
        for company in new_companies:
            session.add(company)
        for job in new_jobs:
            session.add(job)
        existing_sources = {
            (row.source, row.source_record_id): row
            for row in session.scalars(select(CompanySourceRecord))
        }
        for source in source_rows:
            current = existing_sources.get((source.source, source.source_record_id))
            if current is None:
                session.add(source)
                continue
            for field in (
                "company_name", "company_id", "entry_url", "final_url", "status",
                "failure_stage", "reason_code", "reason", "job_count",
                "jd_pending_count", "last_success_job_count", "pagination_complete",
                "last_attempt_at", "updated_at",
            ):
                setattr(current, field, getattr(source, field))
        for detached_target, raw in repairs:
            target = session.get(JobSnapshot, detached_target.id, with_for_update=True)
            if target is None:
                raise RuntimeError(f"repair target disappeared: {detached_target.id}")
            target.jd_raw = str(raw.get("jd_raw") or "").strip()
            target.detail_url = str(raw.get("detail_url") or raw.get("jd_url") or target.detail_url)
            target.capture_status = "complete"
            target.capture_failure_reason = ""
            target.availability_status = "active"
            target.title_key = normalize_job_title_key(target.title)
            target.capture_evidence = dict(raw.get("capture_evidence") or {})
            target.last_seen_at = now
            target.updated_at = now

    with storage.session() as session:
        after_applications = list(session.scalars(select(ApplicationSnapshot)))
        after_digest = _application_digest(after_applications)
        counts = {
            "companies": len(list(session.scalars(select(CompanySnapshot.id)))),
            "jobs": len(list(session.scalars(select(JobSnapshot.id)))),
            "complete_jobs": len(list(session.scalars(select(JobSnapshot.id).where(JobSnapshot.capture_status == "complete")))),
            "failed_jobs": len(list(session.scalars(select(JobSnapshot.id).where(JobSnapshot.capture_status == "failed")))),
        }
    if len(after_applications) != len(applications) or after_digest != application_digest_before:
        raise RuntimeError("application records changed during catalog import")
    report["applications"].update(after_count=len(after_applications), after_sha256=after_digest, unchanged=True)
    report["catalog_after"] = counts
    report["written"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    storage = Storage.from_url(args.database_url)
    try:
        report = import_preview(
            storage,
            args.preview,
            backup=args.backup,
            backup_sha256=args.backup_sha256,
            apply=args.apply,
        )
    finally:
        storage.engine.dispose()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
