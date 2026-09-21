from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
from typing import Any, Mapping

import yaml
from sqlalchemy import select

from packages.config import DEFAULT_OFFERBIU_INDUSTRY_GROUPS, Settings, get_settings
from packages.browser_bridge import BrowserBridgeStore
from packages.domain.models import RecruitmentBatch
from packages.discovery.company_registry import CompanySourceRecord, CompanySourceRegistry
from packages.discovery.offerbiu_refresh import OfferBiuRefreshService
from packages.discovery.reconciliation import normalize_company_name
from packages.recruitment_core.entry import diagnose_candidate_entry
from packages.recruitment_mail import (
    MailRuntimeConfigurationError,
    RecruitmentMailProcessingStatus,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
    sync_configured_mail,
)
from packages.recruitment_mail.processing import process_pending_mail
from packages.repositories.base import RecruitmentRepository
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.matching import DeepSeekClient, MatchingService
from packages.matching.resume import build_analysis_resume_plan, resume_pending_analyses
from packages.orchestration import DailyRecruitmentSync, DailySyncStatus
from packages.pipeline import (
    DailyRecruitmentPipeline,
    MatchingServiceAdapter,
    load_companies,
)
from packages.pipeline.offline import reconcile_offline_jobs
from packages.reporting import build_reporting_summary
from packages.storage import Storage
from packages.storage.models import CompanySnapshot, JobSnapshot
from packages.storage.sync import AgentStateStore
from packages.tools.recruitment_mail import RecruitmentMailReviewInput, review_recruitment_mail
from packages.tools.application_status_update import ApplicationStatusUpdateInput, update_application_status
from packages.tools.typed import read_recruitment_persistence

from .models import TaskCallable, TaskContext
from .runner import _business_failure
from .tasks import TaskType


_SOURCE_STATUS_PRIORITY = {
    "complete": 0,
    "partial": 1,
    "pending": 2,
    "failed": 3,
    "running": 4,
    "unusable": 5,
}

_DAILY_MODES = frozenset({"full", "crawl_only", "score_only"})
_SCOPE_VERSION = 1


def _runtime_dir(settings: Settings) -> Path:
    root = Path(getattr(settings, "agent_root", Path.cwd()))
    directory = root / ".data" / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _scope_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_scope_snapshot(
    settings: Settings,
    run_id: str,
    rows: list[Mapping[str, object]],
    *,
    prefix: str,
) -> Path:
    if not rows:
        raise ValueError("frozen company scope is empty")
    path = _runtime_dir(settings) / f"{prefix}-{run_id}.yaml"
    payload = {
        "version": _SCOPE_VERSION,
        "companies": [dict(row) for row in rows],
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _prepare_configured_scope(
    settings: Settings,
    run_id: str,
    company_ids: tuple[str, ...],
) -> tuple[Path, tuple[str, ...]]:
    companies = load_companies(settings.companies_config)
    requested = set(company_ids)
    missing = sorted(requested - {company.id for company in companies})
    if missing:
        raise ValueError(
            "requested company IDs are not configured: " + ", ".join(missing)
        )
    selected = [company for company in companies if not requested or company.id in requested]
    if not selected:
        raise ValueError("configured company scope is empty")
    path = _write_scope_snapshot(
        settings,
        run_id,
        [company.crawler_config() for company in selected],
        prefix="daily-scope",
    )
    return path, tuple(company.id for company in selected)


def _prepare_saved_company_scope(
    settings: Settings,
    storage: Storage,
    run_id: str,
) -> tuple[Path, tuple[str, ...]]:
    """Freeze the companies represented by saved Agent jobs without discovery."""

    with storage.session() as db:
        company_ids = tuple(
            str(value)
            for value in db.scalars(
                select(JobSnapshot.company_id)
                .where(JobSnapshot.company_id.is_not(None))
                .distinct()
                .order_by(JobSnapshot.company_id)
            )
            if str(value).strip()
        )
        company_rows = {
            row.id: row
            for row in db.scalars(
                select(CompanySnapshot).where(CompanySnapshot.id.in_(company_ids))
            )
        } if company_ids else {}

    if not company_ids:
        raise ValueError("no saved job companies are available for score_only")
    rows: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for company_id in company_ids:
        stored = company_rows.get(company_id)
        base_name = str(getattr(stored, "name", None) or company_id).strip()
        name = base_name
        if name.casefold() in seen_names:
            name = f"{base_name} ({company_id})"
        seen_names.add(name.casefold())
        rows.append(
            {
                "id": company_id,
                "name": name,
                "careers_url": str(getattr(stored, "campus_url", None) or ""),
                "crawler": str(getattr(stored, "crawler_key", None) or ""),
                "integration_status": str(
                    getattr(stored, "integration_status", None) or "not_connected"
                ),
                "aliases": list(getattr(stored, "aliases", None) or []),
                "organization_id": getattr(stored, "organization_id", None),
                "recruitment_unit_name": getattr(stored, "recruitment_unit_name", None),
                "source_identity": getattr(stored, "source_identity", None),
                "scope_source": "saved_job_snapshots",
            }
        )
    path = _write_scope_snapshot(
        settings,
        run_id,
        rows,
        prefix="daily-saved-score-scope",
    )
    return path, company_ids


def _finite_score(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _state_sections(previous: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    state = previous.get("state")
    sections: list[Mapping[str, Any]] = []
    if isinstance(state, Mapping):
        sections.append(state)
        for key in ("metadata", "details"):
            value = state.get(key)
            if isinstance(value, Mapping):
                sections.append(value)
    for key in ("metadata", "details"):
        value = previous.get(key)
        if isinstance(value, Mapping):
            sections.append(value)
    return tuple(sections)


def _state_value(sections: tuple[Mapping[str, Any], ...], *keys: str) -> Any:
    for section in sections:
        for key in keys:
            if key in section and section[key] not in (None, ""):
                return section[key]
    return None


def _load_frozen_resume(
    settings: Settings,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve and validate the persisted scope before any source discovery."""

    sections = _state_sections(previous)
    raw_scope = _state_value(sections, "scope")
    scope_sections: list[Mapping[str, Any]] = []
    if isinstance(raw_scope, Mapping):
        scope_sections.append(raw_scope)
    scope_sections.extend(sections)
    scope_ref = _state_value(
        tuple(scope_sections),
        "snapshot_ref",
        "scope_ref",
        "scope_snapshot",
        "scope_snapshot_ref",
        "companies_path",
    )
    if not scope_ref:
        raise ValueError(
            "resume run has no persisted frozen company scope; refusing full scan"
        )
    scope_path = Path(str(scope_ref)).expanduser()
    if not scope_path.is_file():
        raise ValueError(f"persisted resume scope is missing: {scope_path}")
    stored_digest = _state_value(tuple(scope_sections), "snapshot_digest", "scope_digest")
    if stored_digest and str(stored_digest) != _scope_digest(scope_path):
        raise ValueError(f"persisted resume scope changed: {scope_path}")
    try:
        companies = load_companies(scope_path)
    except Exception as exc:
        raise ValueError(f"persisted resume scope is invalid: {scope_path}") from exc
    if not companies:
        raise ValueError(f"persisted resume scope is empty: {scope_path}")
    stored_ids = _state_value(tuple(scope_sections), "company_ids")
    frozen_ids = tuple(company.id for company in companies)
    if stored_ids:
        requested_ids = tuple(str(value).strip() for value in stored_ids if str(value).strip())
        if requested_ids != frozen_ids:
            raise ValueError("persisted resume company IDs do not match frozen scope")
    source_ids = _state_value(tuple(scope_sections), "source_record_ids") or ()
    checkpoint_ref = _state_value(
        sections,
        "checkpoint_ref",
        "checkpoint_path",
        "checkpoint_snapshot",
    )
    checkpoint_path = Path(str(checkpoint_ref)).expanduser() if checkpoint_ref else None
    if checkpoint_path is not None and not checkpoint_path.is_file():
        raise ValueError(f"persisted resume checkpoint is missing: {checkpoint_path}")
    requested_mode = None
    for mode_key in ("effective_mode", "original_mode", "mode", "requested_mode"):
        candidate_mode = _state_value(sections, mode_key)
        if str(candidate_mode or "").strip() in _DAILY_MODES:
            requested_mode = candidate_mode
            break
    stage = _state_value(sections, "stage", "current_step")
    if not stage:
        stage = previous.get("current_step")
    stage = str(stage or "")
    if stage.startswith("recoverable:"):
        stage = stage.removeprefix("recoverable:")
    requested_mode = str(requested_mode or "").strip()
    if requested_mode not in _DAILY_MODES:
        raise ValueError("resume run has no persisted daily request mode")
    effective_mode = (
        "score_only"
        if requested_mode == "score_only"
        or stage.startswith("matching")
        or stage.startswith("score")
        else requested_mode
    )
    if effective_mode in {"full", "crawl_only"} and checkpoint_path is None:
        raise ValueError(
            "resume run has no persisted crawl checkpoint; refusing to recrawl all companies"
        )
    return {
        "scope_path": scope_path,
        "company_ids": frozen_ids,
        "source_record_ids": tuple(
            str(value).strip() for value in source_ids if str(value).strip()
        ),
        "checkpoint_path": checkpoint_path,
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
    }


def _prepare_full_offerbiu_scope(
    settings: Settings,
    storage: Storage,
    run_id: str,
    source_record_ids: tuple[str, ...],
) -> tuple[Path, int]:
    """Build one full-run catalog from only the current BIU snapshot."""

    with storage.session() as db:
        source_rows = list(db.scalars(select(CompanySourceRecord).where(
            CompanySourceRecord.id.in_(source_record_ids),
            CompanySourceRecord.source == "offerbiu",
        )))

    source_rows.sort(key=lambda row: (
        normalize_company_name(row.company_name),
        _SOURCE_STATUS_PRIORITY.get(row.status, 99),
        row.id,
    ))
    rows: list[dict[str, object]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for record in source_rows:
        diagnosis = diagnose_candidate_entry(record.entry_url)
        if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
            continue
        if not diagnosis.crawler_key:
            continue
        name_key = normalize_company_name(record.company_name)
        if not name_key or name_key in seen_names:
            continue
        company_id = record.company_id or "offerbiu-" + hashlib.sha256(
            record.source_record_id.encode("utf-8")
        ).hexdigest()[:20]
        if company_id in seen_ids:
            continue
        rows.append({
            "id": company_id,
            "name": record.company_name,
            "careers_url": record.entry_url,
            "crawler": diagnosis.crawler_key,
            "integration_status": "connected",
            "recruitment_types": ["秋招"],
            "recruitment_targets": ["2027届"],
            "organization_id": company_id,
            "recruitment_unit_id": company_id,
            "recruitment_unit_name": record.company_name,
            "discovery_source": "offerbiu",
            "source_record_id": record.source_record_id,
            "source_url": record.source_url,
        })
        seen_names.add(name_key)
        seen_ids.add(company_id)

    offerbiu_count = len(rows)
    if not rows:
        raise ValueError(
            "OfferBiu selected industry scope returned zero crawlable sources; "
            "refusing legacy companies fallback"
        )

    runtime_dir = Path(settings.agent_root) / ".data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    scope_path = runtime_dir / f"offerbiu-full-scope-{run_id}.yaml"
    scope_path.write_text(
        yaml.safe_dump({"companies": rows}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return scope_path, offerbiu_count


def _configured_discovery_scope(configured: Any) -> dict[str, list[str]] | None:
    """Read explicit BIU industry selection without importing profile internals."""

    values = getattr(configured, "offerbiu_industry_groups", None)
    if not isinstance(values, (list, tuple, set)):
        return None
    groups = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    return {"industry_groups": groups} if groups else None


def _link_unambiguous_recruitment_mail(
    store: RecruitmentMailStore,
    repository: RecruitmentRepository,
    *,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Link mail and apply only authenticated, uniquely bound progress evidence."""

    categories = [
        category
        for category in RecruitmentMessageCategory
        if category is not RecruitmentMessageCategory.OTHER
    ]
    records = store.query(categories=categories, limit=200)
    applications = {item.id: item for item in repository.list_applications()}
    reviewed = linked = unresolved = approval_previews = updated = unchanged = conflicts = 0
    for record in records:
        if record.processing_status in {
            RecruitmentMailProcessingStatus.IRRELEVANT.value,
            RecruitmentMailProcessingStatus.IGNORED.value,
            RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA.value,
            RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value,
            RecruitmentMailProcessingStatus.FAILED_TERMINAL.value,
        }:
            continue
        if record.processing_status in {
            RecruitmentMailProcessingStatus.PROCESSED_UPDATED.value,
            RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED.value,
        }:
            continue
        category_targets = {
            "application_confirmation": "applied", "assessment": "assessment",
            "written_test": "written", "interview": "interview1",
            "offer": "offer", "rejection": "rejected",
        }
        target = category_targets.get(record.category)
        result = review_recruitment_mail(
            RecruitmentMailReviewInput(record_id=record.id),
            store,
            repository,
        )
        reviewed += 1
        if result.data is None or result.data.association.match is None:
            unresolved += 1
            continue
        association = result.data.association
        match = result.data.association.match
        application = applications.get(match.application_id)
        if application is None:
            unresolved += 1
            continue
        if target is None:
            unresolved += 1
            continue
        update = update_application_status(
            ApplicationStatusUpdateInput(
                application_id=application.id,
                evidence_type="mail",
                evidence_id=record.id,
                target_status=target,
            ),
            repository,
            store,
            settings=settings,
        )
        if update.success and update.data is not None:
            if update.data.state == "updated":
                updated += 1
                store.update_processing_status(record.id, RecruitmentMailProcessingStatus.PROCESSED_UPDATED)
            else:
                unchanged += 1
                store.update_processing_status(
                    record.id,
                    RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED,
                    processing_error=None,
                )
        else:
            conflicts += 1
            persisted = store.get(record_id=record.id)
            terminal_statuses = {
                RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA.value,
                RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value,
                RecruitmentMailProcessingStatus.FAILED_TERMINAL.value,
            }
            if persisted is None or persisted.processing_status not in terminal_statuses:
                store.update_processing_status(
                    record.id,
                    RecruitmentMailProcessingStatus.LINKED
                    if persisted is not None and persisted.application_id
                    else RecruitmentMailProcessingStatus.FAILED,
                    processing_error=str(update.error_code or "status_update_blocked"),
                )
        linked += 1
        approval_previews += len(result.data.approval_previews)
    return {
        "association_reviewed": reviewed,
        "association_linked": linked,
        "association_unresolved": unresolved,
        "approval_previews": approval_previews,
        "updated": updated,
        "unchanged": unchanged,
        "conflicts": conflicts,
    }


def _reporting_inputs(metadata: Mapping[str, object]) -> tuple[object, object, object]:
    """Read optional reporting observations from direct or scheduler metadata."""

    candidates: list[Mapping[str, object]] = [metadata]
    details = metadata.get("details")
    if isinstance(details, Mapping):
        candidates.insert(0, details)
        reporting = details.get("reporting")
        if isinstance(reporting, Mapping):
            candidates.insert(0, reporting)
    reporting = metadata.get("reporting")
    if isinstance(reporting, Mapping):
        candidates.insert(0, reporting)

    def find(keys: tuple[str, ...]) -> object:
        for candidate in candidates:
            for key in keys:
                if key in candidate:
                    return candidate[key]
        return None

    return (
        find(("daily_pipeline_result", "pipeline_result", "daily_result")),
        find(("previous_daily_pipeline_result", "previous_pipeline_result", "previous_result")),
        find(("crawler_baseline", "baseline", "previous_counts")),
    )


def build_runtime_task_handlers(
    *,
    settings: Settings | None = None,
    repository: RecruitmentRepository | None = None,
    mail_store: RecruitmentMailStore | None = None,
    daily_pipeline: DailyRecruitmentPipeline | None = None,
    daily_sync: DailyRecruitmentSync | None = None,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> Mapping[str, TaskCallable]:
    configured = settings or get_settings()
    if repository is None:
        agent_storage = Storage.from_url(configured.database_url)
    elif (
        isinstance(repository, PostgresRecruitmentRepository)
        and daily_pipeline is None
        and daily_sync is None
    ):
        agent_storage = repository.storage
    else:
        agent_storage = None
    repo = repository or PostgresRecruitmentRepository(agent_storage)
    del browser_bridge_store

    def build_matching_service() -> MatchingService | None:
        if not (
            getattr(configured, "llm_enabled", False)
            and getattr(configured, "job_analysis_enabled", False)
        ):
            return None
        client = DeepSeekClient(
            api_key=configured.llm_api_key,
            model=configured.llm_model,
            endpoint=configured.llm_endpoint,
            api_style=configured.model_api_style,
            timeout=configured.llm_timeout_seconds,
            max_tokens=configured.llm_matching_max_tokens,
            thinking_enabled=configured.llm_matching_thinking_enabled,
            reasoning_effort=configured.llm_matching_reasoning_effort,
        )
        return MatchingService(client, max_tokens=configured.llm_matching_max_tokens)

    def build_daily_pipeline(
        company_ids: tuple[str, ...] = (),
        *,
        companies_path: Path | str | None = None,
        checkpoint_path: Path | str | None = None,
        resume_from_checkpoint: bool = False,
    ) -> DailyRecruitmentPipeline:
        if daily_pipeline is not None:
            return daily_pipeline
        if agent_storage is None:
            raise RuntimeError("daily pipeline requires Agent-owned storage")
        profile_path = Path(configured.candidate_profile_config)
        payload = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        profile = payload.get("profile", payload)
        pipeline_kwargs: dict[str, Any] = {}
        if checkpoint_path is not None or resume_from_checkpoint:
            pipeline_kwargs.update(
                {
                    "checkpoint_path": checkpoint_path,
                    "resume_from_checkpoint": resume_from_checkpoint,
                }
            )
        return DailyRecruitmentPipeline(
            companies_path=companies_path or configured.companies_config,
            storage=agent_storage,
            matcher=None,
            profile=profile,
            max_concurrency=configured.crawl_max_concurrency,
            company_timeout_seconds=configured.crawl_company_timeout_seconds,
            match_max_concurrency=int(getattr(configured, "match_max_concurrency", 4)),
            checkpoint_batch_size=int(
                getattr(configured, "match_checkpoint_batch_size", 25)
            ),
            company_ids=company_ids,
            **pipeline_kwargs,
        )

    def prepare_source_scope(
        run_id: str,
        source_record_ids: tuple[str, ...],
    ) -> tuple[Path, tuple[str, ...]]:
        if agent_storage is None:
            raise RuntimeError("source-scoped pipeline requires Agent-owned storage")
        registry = CompanySourceRegistry(agent_storage)
        rows: list[dict[str, object]] = []
        seen_names: set[str] = set()
        for record_id in source_record_ids:
            record = registry.get_source(record_id)
            if record is None:
                raise ValueError(f"OfferBiu source record was not found: {record_id}")
            if str(record.get("source") or "").casefold() != "offerbiu":
                raise ValueError(f"source record is not from OfferBiu: {record_id}")
            if record.get("company_id"):
                raise ValueError(f"OfferBiu source is already registered: {record_id}")
            entry_url = str(record.get("entry_url") or "").strip()
            diagnosis = diagnose_candidate_entry(entry_url)
            if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
                raise ValueError(
                    f"OfferBiu source is not crawlable ({diagnosis.entry_kind}): {record_id}"
                )
            if not diagnosis.crawler_key:
                raise ValueError(f"OfferBiu source has no crawler route: {record_id}")
            company_name = str(record.get("company_name") or "").strip()
            name_key = company_name.casefold()
            if not company_name or name_key in seen_names:
                continue
            seen_names.add(name_key)
            company_id = "offerbiu-" + hashlib.sha256(
                str(record.get("source_record_id") or record_id).encode("utf-8")
            ).hexdigest()[:20]
            rows.append(
                {
                    "id": company_id,
                    "name": company_name,
                    "careers_url": entry_url,
                    "crawler": diagnosis.crawler_key,
                    "integration_status": "connected",
                    "recruitment_types": ["秋招"],
                    "recruitment_targets": ["2027届"],
                    "organization_id": company_id,
                    "recruitment_unit_id": company_id,
                    "recruitment_unit_name": company_name,
                    "discovery_source": "offerbiu",
                    "source_record_id": str(record.get("source_record_id") or ""),
                    "source_url": str(record.get("source_url") or ""),
                }
            )
        if not rows:
            raise ValueError("no unregistered crawlable OfferBiu sources were selected")
        runtime_dir = Path(configured.agent_root) / ".data" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        scope_path = runtime_dir / f"offerbiu-scope-{run_id}.yaml"
        scope_path.write_text(
            yaml.safe_dump({"companies": rows}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return scope_path, tuple(str(row["id"]) for row in rows)

    def build_daily_sync(
        run_id: str | None = None,
        company_ids: tuple[str, ...] = (),
        mode: str = "full",
        source_record_ids: tuple[str, ...] = (),
        *,
        requested_mode: str | None = None,
        frozen_scope_path: Path | str | None = None,
        frozen_company_ids: tuple[str, ...] = (),
        frozen_source_record_ids: tuple[str, ...] = (),
        checkpoint_path: Path | str | None = None,
        resume_from_checkpoint: bool = False,
        resumed_from: str | None = None,
    ) -> DailyRecruitmentSync:
        if daily_sync is not None:
            return daily_sync
        if mode not in _DAILY_MODES:
            raise ValueError(f"unsupported daily mode: {mode}")
        effective_company_ids = tuple(frozen_company_ids or company_ids)
        companies_path: Path | str | None = frozen_scope_path
        persisted_source_record_ids = tuple(
            frozen_source_record_ids or source_record_ids
        )
        if source_record_ids and frozen_scope_path is None:
            if run_id is None:
                raise ValueError("source-scoped pipeline requires a run id")
            companies_path, effective_company_ids = prepare_source_scope(
                run_id, source_record_ids
            )
        elif company_ids and frozen_scope_path is None and agent_storage is not None:
            if run_id is None:
                raise ValueError("company-scoped pipeline requires a run id")
            companies_path, effective_company_ids = _prepare_configured_scope(
                configured,
                run_id,
                tuple(company_ids),
            )
        elif mode == "score_only" and agent_storage is not None and not effective_company_ids:
            if run_id is None:
                raise ValueError("unscoped score_only requires a run id for its local snapshot")
            # Preserve the historical unscoped entry point while making its
            # candidate universe explicit and restartable.  Use saved jobs so
            # BIU-only companies absent from YAML remain scoreable; this is a
            # local read and never refreshes BIU or invokes a crawler.
            companies_path, effective_company_ids = _prepare_saved_company_scope(
                configured,
                agent_storage,
                run_id,
            )
        if resume_from_checkpoint and mode in {"full", "crawl_only"} and not checkpoint_path:
            raise ValueError("resume crawl requires a persisted checkpoint")
        if (
            checkpoint_path is None
            and run_id is not None
            and agent_storage is not None
            and mode in {"full", "crawl_only"}
            and effective_company_ids
        ):
            checkpoint_path = _runtime_dir(configured) / f"daily-checkpoint-{run_id}.json"

        active_scope_path = Path(companies_path).expanduser() if companies_path else None
        current_offerbiu_source_ids = tuple(persisted_source_record_ids)
        offerbiu_company_count = 0
        offerbiu_scope_attempted = False
        state_store = AgentStateStore(agent_storage) if agent_storage is not None else None
        state: dict[str, Any] = {
            "mode": (
                requested_mode
                if requested_mode in _DAILY_MODES
                else mode
            ),
            "original_mode": (
                requested_mode
                if requested_mode in _DAILY_MODES
                else mode
            ),
            "metadata": {
                "requested_mode": (
                    requested_mode
                    if requested_mode in _DAILY_MODES
                    else mode
                ),
                "original_mode": (
                    requested_mode
                    if requested_mode in _DAILY_MODES
                    else mode
                ),
                "effective_mode": mode,
                "company_ids": list(effective_company_ids),
                "source_record_ids": list(persisted_source_record_ids),
                "resumed_from": resumed_from,
            },
            "details": {
                "requested_mode": (
                    requested_mode
                    if requested_mode in _DAILY_MODES
                    else mode
                ),
                "original_mode": (
                    requested_mode
                    if requested_mode in _DAILY_MODES
                    else mode
                ),
                "effective_mode": mode,
                "stage": "starting",
                "scope": {},
                "checkpoint": {},
            },
            "steps": [],
        }

        def _scope_state() -> dict[str, Any]:
            if active_scope_path is None:
                return {
                    "company_ids": list(effective_company_ids),
                    "source_record_ids": list(current_offerbiu_source_ids),
                }
            return {
                "company_ids": list(effective_company_ids),
                "source_record_ids": list(current_offerbiu_source_ids),
                "snapshot_ref": str(active_scope_path.resolve()),
                "snapshot_digest": _scope_digest(active_scope_path),
            }

        def persist_state(stage: str | None = None, *, append_step: bool = False) -> None:
            if stage is not None:
                state["current_step"] = stage
                details = state.setdefault("details", {})
                if isinstance(details, dict):
                    details["stage"] = stage
                    details["scope"] = _scope_state()
                    details["checkpoint"] = (
                        {"checkpoint_ref": str(Path(checkpoint_path).resolve())}
                        if checkpoint_path is not None
                        else {}
                    )
            metadata = state.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["company_ids"] = list(effective_company_ids)
                metadata["source_record_ids"] = list(current_offerbiu_source_ids)
                if active_scope_path is not None:
                    metadata["scope_ref"] = str(active_scope_path.resolve())
                    metadata["scope_digest"] = _scope_digest(active_scope_path)
                if checkpoint_path is not None:
                    metadata["checkpoint_ref"] = str(Path(checkpoint_path).resolve())
            if append_step and stage is not None:
                steps = state.setdefault("steps", [])
                if isinstance(steps, list):
                    steps.append({"stage": stage})
                    del steps[:-200]
            if state_store is not None and run_id is not None:
                state_store.save_task_state(run_id, state, ensure_task_run=True)

        def set_scope(
            scope_path: Path | str,
            company_scope_ids: tuple[str, ...],
            source_scope_ids: tuple[str, ...] = (),
        ) -> None:
            nonlocal active_scope_path, effective_company_ids
            nonlocal current_offerbiu_source_ids, checkpoint_path
            active_scope_path = Path(scope_path).expanduser()
            effective_company_ids = tuple(company_scope_ids)
            current_offerbiu_source_ids = tuple(source_scope_ids)
            if (
                checkpoint_path is None
                and run_id is not None
                and agent_storage is not None
                and mode in {"full", "crawl_only"}
            ):
                checkpoint_path = _runtime_dir(configured) / f"daily-checkpoint-{run_id}.json"
            persist_state("scope_frozen", append_step=True)

        if active_scope_path is not None:
            set_scope(
                active_scope_path,
                tuple(effective_company_ids),
                current_offerbiu_source_ids,
            )
        else:
            persist_state("scope_pending", append_step=True)

        def progress(stage: str, completed: int, total: int) -> None:
            value = f"{stage}:{completed}/{total}"
            if state_store is not None and run_id is not None:
                state_store.update_task_progress(run_id, value)
            persist_state(value, append_step=True)

        def build_pipeline() -> DailyRecruitmentPipeline:
            result = build_daily_pipeline(
                tuple(effective_company_ids),
                companies_path=active_scope_path,
                checkpoint_path=checkpoint_path,
                resume_from_checkpoint=resume_from_checkpoint,
            )
            if state_store is not None and run_id is not None:
                result.progress_callback = progress
            return result

        pipeline = build_pipeline()

        def discover(dry_run: bool = False):
            nonlocal current_offerbiu_source_ids, offerbiu_company_count, pipeline
            nonlocal offerbiu_scope_attempted
            if effective_company_ids:
                return {"source": "offerbiu", "status": "skipped_scoped_run"}
            if not getattr(configured, "discovery_enabled", False):
                return {"source": "offerbiu", "status": "disabled"}
            if agent_storage is None:
                return {"source": "offerbiu", "status": "storage_unavailable"}
            offerbiu_scope_attempted = True
            configured_scope = _configured_discovery_scope(configured)
            service_parameters = inspect.signature(OfferBiuRefreshService).parameters
            if configured_scope and "scope" in service_parameters:
                service = OfferBiuRefreshService(
                    CompanySourceRegistry(agent_storage),
                    scope=configured_scope,
                )
            elif configured_scope and set(configured_scope["industry_groups"]) != set(
                DEFAULT_OFFERBIU_INDUSTRY_GROUPS
            ):
                raise RuntimeError(
                    "当前 OfferBiu 发现适配器不支持所选行业范围；已拒绝回退默认或 legacy 公司清单"
                )
            else:
                service = OfferBiuRefreshService(CompanySourceRegistry(agent_storage))
            result = service.refresh(
                apply=not dry_run,
                max_pages=int(getattr(configured, "offerbiu_max_pages", 150)),
                page_size=int(getattr(configured, "offerbiu_page_size", 50)),
                delay_seconds=float(getattr(configured, "offerbiu_delay_seconds", 0.05)),
            )
            if not result.get("complete"):
                raise RuntimeError(
                    f"OfferBiu source refresh incomplete: {result.get('error') or 'unknown error'}"
                )
            current_offerbiu_source_ids = service.last_registered_ids
            if current_offerbiu_source_ids and run_id is not None:
                full_scope_path, offerbiu_company_count = _prepare_full_offerbiu_scope(
                    configured,
                    agent_storage,
                    run_id,
                    current_offerbiu_source_ids,
                )
                scope_ids = tuple(
                    company.id for company in load_companies(full_scope_path)
                )
                set_scope(full_scope_path, scope_ids, current_offerbiu_source_ids)
                pipeline = build_pipeline()
            return {
                "source": "offerbiu",
                "status": "preview" if dry_run else "registered",
                "industry_groups": (
                    list(configured_scope["industry_groups"])
                    if configured_scope
                    else list(DEFAULT_OFFERBIU_INDUSTRY_GROUPS)
                ),
                **result,
            }

        def reconcile(source_results):
            return {
                "source": "offerbiu",
                "status": source_results.get("status", "unknown"),
                "new_count": int(source_results.get("new_entries") or 0),
                "registered_entries": int(source_results.get("registered_entries") or 0),
                "excluded_unusable": int(source_results.get("excluded_unusable") or 0),
            }

        def score_existing(dry_run: bool) -> dict[str, object]:
            # Matching is its own restartable stage; persist it before reading
            # the plan so a crash cannot resume the crawl stage by accident.
            persist_state("matching", append_step=True)

            def matching_progress(value: Any) -> None:
                progress_value = f"matching:{value.processed}/{value.planned}"
                if state_store is not None and run_id is not None:
                    state_store.update_task_progress(run_id, progress_value)
                persist_state(progress_value, append_step=True)

            service = build_matching_service()
            if service is None or agent_storage is None:
                return {
                    "analysis_enabled": False,
                    "scoring_candidates": 0,
                    "scored": 0,
                    "scoring_failed": 0,
                    "unscored": 0,
                    "score_only": True,
                    "written": False,
                }
            profile_path = Path(configured.candidate_profile_config)
            profile_payload = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
            profile = profile_payload.get("profile", profile_payload)
            plan = build_analysis_resume_plan(
                agent_storage,
                profile,
                company_ids=effective_company_ids,
            )
            pending_jobs = tuple(
                candidate
                for candidate in plan.pending_jobs
                if not _finite_score(candidate.job.match_score)
            )
            if dry_run:
                return {
                    "analysis_enabled": service is not None,
                    "scoring_candidates": len(pending_jobs),
                    "scored": 0,
                    "scoring_failed": 0,
                    "unscored": len(pending_jobs),
                    "score_only": True,
                    "written": False,
                    "plan": {**plan.as_dict(), "pending_jobs": len(pending_jobs)},
                }
            result = resume_pending_analyses(
                agent_storage,
                profile,
                service,
                pending_jobs,
                concurrency=int(getattr(configured, "match_max_concurrency", 1)),
                progress=matching_progress,
            )
            return {
                "analysis_enabled": True,
                "scoring_candidates": result.planned,
                "scored": result.completed,
                "scoring_failed": result.failed + result.refused,
                "unscored": max(0, result.planned - result.completed),
                "failure_reasons": dict(result.errors),
                "score_only": True,
                "written": result.processed > 0,
                "score_result": result.as_dict(),
            }

        def crawl(dry_run: bool):
            nonlocal pipeline, active_scope_path, effective_company_ids
            nonlocal checkpoint_path, offerbiu_company_count
            if mode == "score_only":
                return score_existing(dry_run)
            active_pipeline = pipeline
            if (
                not effective_company_ids
                and current_offerbiu_source_ids
                and agent_storage is not None
                and run_id is not None
            ):
                full_scope_path, offerbiu_company_count = _prepare_full_offerbiu_scope(
                    configured,
                    agent_storage,
                    run_id,
                    current_offerbiu_source_ids,
                )
                scope_ids = tuple(
                    company.id for company in load_companies(full_scope_path)
                )
                set_scope(full_scope_path, scope_ids, current_offerbiu_source_ids)
                active_pipeline = build_pipeline()
                pipeline = active_pipeline
            elif active_scope_path is None and agent_storage is not None and run_id is not None:
                if offerbiu_scope_attempted:
                    raise ValueError(
                        "OfferBiu selected industry scope returned zero crawlable sources; "
                        "refusing legacy companies fallback"
                    )
                fallback_path, fallback_ids = _prepare_configured_scope(
                    configured,
                    run_id,
                    (),
                )
                set_scope(fallback_path, fallback_ids, current_offerbiu_source_ids)
                active_pipeline = build_pipeline()
                pipeline = active_pipeline
            result = active_pipeline.run(dry_run=dry_run)
            converter = getattr(result, "to_dict", None)
            payload = converter() if callable(converter) else result
            if isinstance(payload, dict):
                payload = dict(payload)
                if offerbiu_company_count:
                    payload["offerbiu_companies_queued"] = offerbiu_company_count
                payload.setdefault("written", bool(getattr(result, "written", False)))
                if mode == "full":
                    scoring = score_existing(dry_run)
                    payload.update({
                        key: scoring[key]
                        for key in (
                            "analysis_enabled", "scoring_candidates", "scored",
                            "scoring_failed", "unscored", "score_result",
                        )
                        if key in scoring
                    })
                    failures = Counter(payload.get("failure_reasons") or {})
                    failures.update(scoring.get("failure_reasons") or {})
                    payload["failure_reasons"] = dict(failures)
                    payload["written"] = bool(payload.get("written") or scoring.get("written"))
            return payload

        def report(pipeline_result, reconciliation_result, offline_result):
            payload = build_reporting_summary(
                pipeline_result,
                repository=repo,
            )
            payload["company_reconciliation"] = reconciliation_result
            payload["offline_reconciliation"] = offline_result
            return payload

        def offline_reconcile(pipeline_result, dry_run: bool):
            if not getattr(configured, "offline_reconciliation_enabled", False):
                return {"status": "disabled", "written": False}
            if agent_storage is None:
                return {"status": "unavailable", "written": False}
            return reconcile_offline_jobs(
                agent_storage,
                pipeline_result,
                observed_at=datetime.now(timezone.utc),
                dry_run=dry_run,
                grace_runs=int(getattr(configured, "offline_grace_runs", 2)),
                grace_days=float(getattr(configured, "offline_grace_days", 3.0)),
            )

        return DailyRecruitmentSync(
            discovery=(
                discover
                if mode in {"full", "crawl_only"}
                and not resume_from_checkpoint
                and getattr(configured, "discovery_enabled", False)
                else None
            ),
            reconcile=(
                reconcile
                if mode in {"full", "crawl_only"}
                and not resume_from_checkpoint
                and getattr(configured, "discovery_enabled", False)
                else None
            ),
            crawl=crawl,
            offline_reconcile=offline_reconcile if mode in {"full", "crawl_only"} else None,
            report=report,
            state_store=state_store,
        )

    def daily_recruitment_intelligence(context: TaskContext) -> dict[str, object]:
        if daily_sync is not None or daily_pipeline is not None or repository is None:
            if not context.write_enabled:
                raise PermissionError("Agent database write is not authorized for this task")
            details = context.metadata.get("details")
            requested_dry_run = bool(
                isinstance(details, Mapping) and details.get("requested_dry_run")
            )
            company_ids = tuple(
                str(value).strip()
                for value in (details.get("company_ids") if isinstance(details, Mapping) else []) or []
                if str(value).strip()
            )
            source_record_ids = tuple(
                str(value).strip()
                for value in (
                    details.get("source_record_ids")
                    if isinstance(details, Mapping)
                    else []
                ) or []
                if str(value).strip()
            )
            requested_mode = (
                str(details.get("mode") or "full").strip().casefold()
                if isinstance(details, Mapping)
                else "full"
            )
            resume_run_id = str(details.get("resume_run_id") or "") if isinstance(details, Mapping) else ""
            effective_mode = requested_mode
            frozen_scope_path: Path | None = None
            frozen_company_ids: tuple[str, ...] = ()
            frozen_source_record_ids: tuple[str, ...] = ()
            checkpoint_path: Path | None = None
            resumed_from: str | None = None
            if requested_mode == "resume":
                if company_ids or source_record_ids:
                    raise ValueError(
                        "resume mode cannot override the persisted company scope"
                    )
                if not resume_run_id:
                    raise ValueError("resume mode requires resume_run_id")
                if agent_storage is None:
                    raise RuntimeError(
                        "resume requires Agent-owned state storage"
                    )
                previous = AgentStateStore(agent_storage).get_task_run(resume_run_id)
                if previous is None:
                    raise ValueError(f"resume run was not found: {resume_run_id}")
                resume_info = _load_frozen_resume(configured, previous)
                effective_mode = str(resume_info["effective_mode"])
                frozen_scope_path = Path(resume_info["scope_path"])
                frozen_company_ids = tuple(resume_info["company_ids"])
                frozen_source_record_ids = tuple(resume_info["source_record_ids"])
                checkpoint_path = resume_info["checkpoint_path"]
                resumed_from = resume_run_id
            elif resume_run_id:
                raise ValueError("resume_run_id is only valid for resume mode")
            elif requested_mode not in _DAILY_MODES:
                raise ValueError(f"unsupported daily mode: {requested_mode}")
            if getattr(configured, "env", "development") == "desktop-isolated" and effective_mode in {"full", "crawl_only"}:
                from packages.candidate_profile.loader import CandidateProfileError, load_candidate_profile
                try:
                    candidate = load_candidate_profile(configured.candidate_profile_config)
                    keywords = candidate.matching.title_keywords
                except CandidateProfileError:
                    keywords = []
                missing = []
                if not keywords:
                    missing.append("title_keywords")
                if not configured.offerbiu_industry_groups:
                    missing.append("industry_groups")
                if missing:
                    return {"status": "configuration_required", "missing": missing,
                            "agent_write_performed": False,
                            "message": "请先配置岗位标题关键词和行业范围；助理聊天不受此限制。"}
            result = build_daily_sync(
                context.run_id,
                () if requested_mode == "resume" else company_ids,
                effective_mode,
                () if requested_mode == "resume" else source_record_ids,
                requested_mode=(
                    resume_info["requested_mode"]
                    if requested_mode == "resume"
                    else requested_mode
                ),
                frozen_scope_path=frozen_scope_path,
                frozen_company_ids=frozen_company_ids,
                frozen_source_record_ids=frozen_source_record_ids,
                checkpoint_path=checkpoint_path,
                resume_from_checkpoint=(
                    requested_mode == "resume"
                    and effective_mode in {"full", "crawl_only"}
                ),
                resumed_from=resumed_from,
            ).run(
                run_id=context.run_id,
                dry_run=requested_dry_run,
            )
            payload = result.model_dump(mode="json")
            pipeline_payload = payload.get("pipeline")
            pipeline_metrics = pipeline_payload if isinstance(pipeline_payload, dict) else {}
            discovery = payload.get("discovery")
            discovery_metrics = discovery if isinstance(discovery, dict) else {}
            offline = payload.get("offline_reconciliation")
            offline_metrics = offline if isinstance(offline, dict) else {}
            source_registration_written = bool(
                discovery_metrics.get("applied")
                and discovery_metrics.get("registered_entries", 0)
            )
            write_statistics = {
                "source_registration_write_performed": source_registration_written,
                "source_registered_entry_count": discovery_metrics.get("registered_entries"),
                "pipeline_write_performed": bool(pipeline_metrics.get("written")),
                "job_snapshot_write_count": None,
                "offline_reconciliation_write_performed": bool(offline_metrics.get("written")),
                "basis": (
                    "Reported business-stage writes, excluding scheduler/checkpoint bookkeeping. "
                    "Pipeline writes may include job snapshots and analysis; not a job row count. "
                    "Per-run job write count is unknown without a row-level write receipt. "
                    "False means no write receipt, not proof that no partial writes occurred. "
                    "Registered entries count source rows, not distinct companies."
                ),
            }
            response = {
                **pipeline_metrics,
                "status": (
                    "failed" if result.status is DailySyncStatus.FAILED
                    or _business_failure(pipeline_metrics) else "completed"
                ),
                "sync_status": result.status.value,
                "daily_sync": payload,
                "error": payload.get("error") or _business_failure(pipeline_metrics),
                "agent_write_performed": any((
                    source_registration_written,
                    write_statistics["pipeline_write_performed"],
                    write_statistics["offline_reconciliation_write_performed"],
                )),
                "write_statistics": write_statistics,
                "persistence": read_recruitment_persistence(
                    agent_storage or getattr(repo, "storage", None),
                ).model_dump(mode="json"),
                "source_write_attempted": False,
                "source_write_scope": "legacy_project_read_only",
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "source_record_ids": list(
                    frozen_source_record_ids
                    if requested_mode == "resume"
                    else source_record_ids
                ),
                "resumed_from": resume_run_id or None,
            }
            if agent_storage is not None and not requested_dry_run:
                state_store = AgentStateStore(agent_storage)
                try:
                    state = state_store.get_task_state(context.run_id) or {}
                    state_store.save_task_state(context.run_id, {**state, "result": response})
                except Exception as exc:
                    # Keep the original receipt even if final bookkeeping fails.
                    response["result_persistence_error"] = type(exc).__name__
            return response
        page = repo.search_jobs(
            cohort=2027,
            cohort_status="confirmed",
            first_seen_on=context.scheduled_for.date(),
            batches=(RecruitmentBatch.FORMAL, RecruitmentBatch.EARLY),
            limit=20,
            offset=0,
        )
        return {
            "status": "observed",
            "confirmed_2027_new_jobs": page.total,
            "sample_job_ids": [item.id for item in page.items],
            "source_write_attempted": False,
        }

    def crawler_health(context: TaskContext) -> dict[str, object]:
        companies = repo.list_companies()
        counts = Counter(item.integration_status for item in companies)
        unresolved = [
            item.id
            for item in companies
            if item.integration_status != "connected"
        ][:50]
        current_result, previous_result, baseline = _reporting_inputs(context.metadata)
        report = build_reporting_summary(
            current_result,
            previous_result=previous_result,
            baseline=baseline,
            repository=repo,
            companies=companies,
            run_id=context.run_id,
            scheduled_for=context.scheduled_for,
        )
        return {
            "status": "observed",
            "company_total": len(companies),
            "integration_status_counts": dict(sorted(counts.items())),
            "unresolved_company_ids": unresolved,
            "live_crawler_run_attempted": False,
            "source_write_attempted": False,
            "report": report,
            "daily_summary": report["daily_summary"],
            "crawler_health": report["crawler_health"],
        }

    def application_progress(_context: TaskContext) -> dict[str, object]:
        applications = repo.list_applications()
        pages = {item.record_url for item in applications if item.record_url}
        return {
            "status": "waiting_browser" if pages else "no_reviewable_pages",
            "application_count": len(applications),
            "reviewable_page_count": len(pages),
            "missing_record_url_count": sum(not item.record_url for item in applications),
            "browser_navigation_attempted": False,
            "source_write_attempted": False,
        }

    def recruitment_mailbox(context: TaskContext) -> dict[str, object]:
        if not configured.mail_enabled:
            return {
                "status": "disabled",
                "reason": "RECRUITOPS_MAIL_ENABLED is false",
                "source_write_attempted": False,
            }
        store = mail_store or RecruitmentMailStore(Storage.from_url(configured.database_url))
        try:
            result = sync_configured_mail(
                configured,
                store,
                run_id=context.run_id,
                operation_id=f"mail-sync:{context.run_id}:attempt-{context.attempt}",
                attempts=context.attempt,
            )
        except MailRuntimeConfigurationError as exc:
            return {
                "status": "configuration_required",
                "reason": str(exc),
                "source_write_attempted": False,
            }
        processing_summary = dict(
            process_pending_mail(store, repo, configured, limit=20)
        )
        updated = int(processing_summary.get("updated", 0))
        unchanged = int(processing_summary.get("unchanged", 0))
        unresolved = int(processing_summary.get("unresolved", 0))
        return {
            "status": "synced",
            **result.model_dump(mode="json"),
            "processing": processing_summary,
            "processing_status": processing_summary.get("status"),
            **{
                key: value
                for key, value in processing_summary.items()
                if key != "status"
            },
            "association_reviewed": processing_summary.get("processed", 0),
            "association_linked": updated + unchanged,
            "association_unresolved": unresolved,
            "approval_previews": 0,
            "conflicts": processing_summary.get("conflicts", 0),
            "source_write_attempted": False,
        }

    return {
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: daily_recruitment_intelligence,
        TaskType.CRAWLER_HEALTH.value: crawler_health,
        TaskType.APPLICATION_PROGRESS.value: application_progress,
        TaskType.RECRUITMENT_MAILBOX.value: recruitment_mailbox,
    }


__all__ = ["build_runtime_task_handlers"]
