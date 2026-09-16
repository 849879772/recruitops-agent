"""Reconcile jobs that disappear from a complete company crawl.

The Agent snapshot schema deliberately has no job-visibility column.  This
module keeps the small amount of reconciliation state in a versioned prefix of
``JobSnapshot.source_ref``.  The original source reference remains at the end
of the value, so existing fingerprint suffixes continue to work.  A source
reference that cannot fit the marker is handled as a returned plan only.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from packages.storage import JobSnapshot, Storage


UTC = timezone.utc
SOURCE_REF_PREFIX = "recruitops-offline:v1:"
INACTIVE_SOURCE_REF_PREFIX = "recruitops-offline:v1:inactive:"
SOURCE_REF_MAX_LENGTH = 512
DEFAULT_GRACE_RUNS = 2
DEFAULT_GRACE_DAYS = 3.0
_MISSING = object()


class JobVisibility(StrEnum):
    """Visibility represented by an offline reconciliation observation."""

    ACTIVE = "active"
    MISSING = "missing"
    INACTIVE = "inactive"


class ReconciliationAction(StrEnum):
    """Action proposed for one persisted job snapshot."""

    OBSERVED = "observed"
    MISSING = "missing"
    INACTIVE = "inactive"
    RESTORED = "restored"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CompanyRunObservation:
    """The bounded evidence needed to reconcile one company.

    ``observed_job_ids`` must come from the same complete crawl represented by
    this row.  A failed or incomplete row is retained in the result as a skip,
    but never contributes a missing observation.
    """

    company_id: str
    observed_job_ids: frozenset[str] = frozenset()
    status: str = "completed"
    pagination_complete: bool = True
    has_more: bool = False
    failure_reason: str | None = None
    run_reason: str = "completed"
    success: bool | None = None
    observations_available: bool = True

    @property
    def eligible(self) -> bool:
        return _company_is_eligible(self)[0]


@dataclass(frozen=True, slots=True)
class OfflineJobState:
    """Decoded visibility state carried by a job ``source_ref``."""

    status: str = JobVisibility.ACTIVE.value
    missing_runs: int = 0
    missing_since: datetime | None = None
    last_missing_at: datetime | None = None
    original_source_ref: str | None = None
    encoded: bool = False
    valid: bool = True


@dataclass(frozen=True, slots=True)
class JobReconciliationPlan:
    """A dry-run-safe decision for one persisted job."""

    job_id: str
    company_id: str
    action: str
    previous_status: str
    status: str
    missing_runs: int
    missing_since: datetime | None
    last_missing_at: datetime | None
    last_seen_at: datetime | None
    age_days: float | None
    grace_runs_met: bool
    grace_days_met: bool
    persistable: bool
    will_write: bool
    reason: str

    @property
    def transition(self) -> bool:
        return self.action in {
            ReconciliationAction.INACTIVE.value,
            ReconciliationAction.RESTORED.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company_id": self.company_id,
            "action": self.action,
            "previous_status": self.previous_status,
            "status": self.status,
            "missing_runs": self.missing_runs,
            "missing_since": _json_datetime(self.missing_since),
            "last_missing_at": _json_datetime(self.last_missing_at),
            "last_seen_at": _json_datetime(self.last_seen_at),
            "age_days": self.age_days,
            "grace_runs_met": self.grace_runs_met,
            "grace_days_met": self.grace_days_met,
            "persistable": self.persistable,
            "will_write": self.will_write,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OfflineReconciliationResult:
    """Summary and returned plan for one offline reconciliation run."""

    observed_at: datetime
    grace_runs: int
    grace_days: float
    dry_run: bool
    written: bool
    processed_company_ids: tuple[str, ...] = ()
    skipped_company_ids: tuple[str, ...] = ()
    plans: tuple[JobReconciliationPlan, ...] = ()
    observed_count: int = 0
    missing_count: int = 0
    inactive_count: int = 0
    restored_count: int = 0
    unchanged_count: int = 0
    skipped_count: int = 0
    planned_only_count: int = 0

    @property
    def changes(self) -> tuple[JobReconciliationPlan, ...]:
        return self.plans

    @property
    def plan(self) -> tuple[JobReconciliationPlan, ...]:
        return self.plans

    @property
    def jobs(self) -> tuple[JobReconciliationPlan, ...]:
        return self.plans

    @property
    def processed_company_count(self) -> int:
        return len(self.processed_company_ids)

    @property
    def skipped_company_count(self) -> int:
        return len(self.skipped_company_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": _json_datetime(self.observed_at),
            "grace_runs": self.grace_runs,
            "grace_days": self.grace_days,
            "dry_run": self.dry_run,
            "written": self.written,
            "processed_company_ids": list(self.processed_company_ids),
            "skipped_company_ids": list(self.skipped_company_ids),
            "processed_company_count": self.processed_company_count,
            "skipped_company_count": self.skipped_company_count,
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "inactive_count": self.inactive_count,
            "restored_count": self.restored_count,
            "unchanged_count": self.unchanged_count,
            "skipped_count": self.skipped_count,
            "planned_only_count": self.planned_only_count,
            "plans": [item.to_dict() for item in self.plans],
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class _CompanyInput:
    observation: CompanyRunObservation
    eligible: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class _JobDecision:
    plan: JobReconciliationPlan
    new_source_ref: str | None
    new_last_seen_at: datetime | None
    new_updated_at: datetime | None


def _json_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _field(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _coerce_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO datetime") from exc
    raise TypeError(f"{field_name} must be a datetime")


def _value_of_enum(value: Any) -> Any:
    return getattr(value, "value", value)


def _coerce_ids(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)):
        text = _text(value)
        return frozenset({text}) if text else frozenset()
    if isinstance(value, Mapping):
        nested = _field(value, ("id", "job_id", "jobId"), _MISSING)
        if nested is not _MISSING:
            text = _text(nested)
            return frozenset({text}) if text else frozenset()
        return frozenset()
    try:
        iterator = iter(value)
    except TypeError:
        text = _text(_field(value, ("id", "job_id", "jobId"), value))
        return frozenset({text}) if text else frozenset()

    ids: set[str] = set()
    for item in iterator:
        if isinstance(item, (str, bytes)):
            item_id = _text(item)
        else:
            item_id = _text(_field(item, ("id", "job_id", "jobId"), item))
        if item_id:
            ids.add(item_id)
    return frozenset(ids)


def _observed_index(value: Any) -> dict[str, frozenset[str]]:
    """Normalize either ``company -> ids`` or row-shaped observations."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        for wrapper in (
            "observed_jobs_by_company",
            "observed_job_ids",
            "jobs_by_company",
            "observations",
        ):
            nested = value.get(wrapper)
            if nested is not None and nested is not value:
                return _observed_index(nested)
        result: dict[str, frozenset[str]] = {}
        for company_id, rows in value.items():
            company = _text(company_id)
            if company:
                result[company] = _coerce_ids(rows)
        return result
    if isinstance(value, (str, bytes)):
        return {}

    result: dict[str, set[str]] = {}
    try:
        iterator = iter(value)
    except TypeError:
        return {}
    for row in iterator:
        company_id = _text(
            _field(row, ("company_id", "companyId", "company", "company_name"), "")
        )
        if not company_id:
            continue
        rows = _field(
            row,
            ("observed_job_ids", "observed_ids", "job_ids", "observed_jobs", "jobs"),
            _MISSING,
        )
        if rows is _MISSING:
            rows = _field(row, ("id", "job_id", "jobId"), None)
        result.setdefault(company_id, set()).update(_coerce_ids(rows))
    return {company_id: frozenset(ids) for company_id, ids in result.items()}


def _container(value: Any) -> Any:
    if value is None:
        return None
    nested = _field(value, ("company_runs", "company_results", "companies", "runs"), _MISSING)
    return None if nested is _MISSING else nested


def _run_entries(value: Any) -> list[tuple[str | None, Any]]:
    if value is None:
        return []
    nested = _container(value)
    if nested is not None and nested is not value:
        return _run_entries(nested)
    if isinstance(value, Mapping):
        if _field(value, ("company_id", "companyId", "company"), _MISSING) is not _MISSING:
            return [(None, value)]
        return [(_text(key), item) for key, item in value.items()]
    if isinstance(value, (str, bytes)):
        return [(_text(value), None)]
    try:
        return [(None, item) for item in value]
    except TypeError:
        return [(None, value)]


def _entry_to_observation(
    keyed_company_id: str | None,
    value: Any,
    observed: Mapping[str, frozenset[str]],
) -> CompanyRunObservation | None:
    if isinstance(value, CompanyRunObservation):
        return value

    company_id = _text(
        _field(value, ("company_id", "companyId", "company", "company_name"), "")
    ) or _text(keyed_company_id)
    if not company_id:
        return None

    if isinstance(value, (str, bytes)) or value is None or isinstance(value, bool):
        observed_ids = observed.get(company_id, frozenset())
        available = company_id in observed
        success = value if isinstance(value, bool) else None
        return CompanyRunObservation(
            company_id=company_id,
            observed_job_ids=observed_ids,
            success=success,
            observations_available=available,
        )

    rows = _field(
        value,
        ("observed_job_ids", "observed_ids", "job_ids", "observed_jobs", "jobs"),
        _MISSING,
    )
    available = rows is not _MISSING
    if available:
        observed_ids = _coerce_ids(rows)
    else:
        observed_ids = observed.get(company_id, frozenset())
        available = company_id in observed

    success_value = _field(value, ("success", "succeeded"), None)
    success = success_value if isinstance(success_value, bool) else None
    status = _text(_value_of_enum(_field(value, ("status",), "completed"))) or "completed"
    pagination_value = _field(value, ("pagination_complete", "paginationComplete", "complete"), True)
    has_more = bool(_field(value, ("has_more", "hasMore"), False))
    failure_reason = _field(value, ("failure_reason", "failureReason", "error"), None)
    run_reason = _text(_field(value, ("run_reason", "runReason"), "completed"))
    pages_seen = _field(value, ("pages_seen", "pagesSeen"), _MISSING)
    total_pages = _field(value, ("total_pages", "totalPages"), _MISSING)
    if pages_seen is not _MISSING and total_pages is not _MISSING:
        try:
            if total_pages is not None and int(pages_seen) < int(total_pages):
                pagination_value = False
        except (TypeError, ValueError):
            pagination_value = False

    return CompanyRunObservation(
        company_id=company_id,
        observed_job_ids=observed_ids,
        status=status,
        pagination_complete=bool(pagination_value),
        has_more=has_more,
        failure_reason=_text(failure_reason) or None,
        run_reason=run_reason,
        success=success,
        observations_available=available,
    )


def _company_inputs(
    *,
    run: Any,
    company_runs: Any,
    successful_companies: Any,
    observed: Mapping[str, frozenset[str]],
) -> tuple[tuple[_CompanyInput, ...], tuple[str, ...]]:
    source = company_runs
    if source is None:
        source = _container(run)
    entries = _run_entries(source)

    if not entries and successful_companies is not None:
        entries = _run_entries(successful_companies)
    if not entries and observed:
        entries = [(company_id, None) for company_id in observed]

    by_company: dict[str, _CompanyInput] = {}
    for keyed_id, value in entries:
        item = _entry_to_observation(keyed_id, value, observed)
        if item is None:
            continue
        eligible, reason = _company_is_eligible(item)
        existing = by_company.get(item.company_id)
        candidate = _CompanyInput(item, eligible, reason)
        if existing is None:
            by_company[item.company_id] = candidate
            continue
        # A duplicate company is safe only when every duplicate is safe.  This
        # prevents one failed page from being hidden by a successful duplicate.
        merged = CompanyRunObservation(
            company_id=item.company_id,
            observed_job_ids=existing.observation.observed_job_ids | item.observed_job_ids,
            status=(
                item.status
                if existing.eligible and eligible
                else "failed"
            ),
            pagination_complete=existing.observation.pagination_complete and item.pagination_complete,
            has_more=existing.observation.has_more or item.has_more,
            failure_reason=existing.observation.failure_reason or item.failure_reason,
            run_reason=existing.observation.run_reason or item.run_reason,
            success=(
                True
                if existing.eligible and eligible
                else False
            ),
            observations_available=(
                existing.observation.observations_available
                or item.observations_available
            ),
        )
        merged_eligible, merged_reason = _company_is_eligible(merged)
        by_company[item.company_id] = _CompanyInput(merged, merged_eligible, merged_reason)

    processed = tuple(sorted(company_id for company_id, item in by_company.items() if item.eligible))
    skipped = tuple(sorted(company_id for company_id, item in by_company.items() if not item.eligible))
    return tuple(by_company.values()), skipped


def _company_is_eligible(observation: CompanyRunObservation) -> tuple[bool, str | None]:
    status = _text(_value_of_enum(observation.status)).casefold()
    if observation.success is False or status in {"failed", "failure", "error", "skipped"}:
        return False, "crawl_failed"
    if observation.failure_reason:
        return False, observation.failure_reason
    if not observation.pagination_complete or observation.has_more:
        return False, "pagination_incomplete"
    run_reason = observation.run_reason.casefold()
    if any(token in run_reason for token in ("pagination_incomplete", "partial", "incomplete")):
        return False, "pagination_incomplete"
    if not observation.observations_available:
        return False, "observed_jobs_missing"
    return True, None


def _decode_payload(token: str) -> Mapping[str, Any] | None:
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(f"{token}{padding}")
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def decode_offline_state(source_ref: str | None) -> OfflineJobState:
    """Decode a job state without raising for an ordinary source reference."""

    if not source_ref or not source_ref.startswith(SOURCE_REF_PREFIX):
        return OfflineJobState(original_source_ref=source_ref)
    remainder = source_ref[len(SOURCE_REF_PREFIX) :]
    explicit_status = None
    status_token, separator, tail = remainder.partition(":")
    if separator and status_token in {item.value for item in JobVisibility}:
        explicit_status = status_token
        remainder = tail
    token, separator, original = remainder.partition(":")
    if not separator:
        return OfflineJobState(encoded=True, valid=False)
    payload = _decode_payload(token)
    if payload is None:
        return OfflineJobState(encoded=True, valid=False)
    status = explicit_status or _text(payload.get("s"))
    try:
        missing_runs = int(payload.get("r", 0))
    except (TypeError, ValueError):
        return OfflineJobState(encoded=True, valid=False)
    if status not in {item.value for item in JobVisibility} or missing_runs < 1:
        return OfflineJobState(encoded=True, valid=False)
    try:
        missing_since = _coerce_datetime(payload.get("f"), field_name="missing_since")
        last_missing_at = _coerce_datetime(payload.get("a"), field_name="last_missing_at")
    except (TypeError, ValueError):
        return OfflineJobState(encoded=True, valid=False)
    if missing_since is None or last_missing_at is None:
        return OfflineJobState(encoded=True, valid=False)
    return OfflineJobState(
        status=status,
        missing_runs=missing_runs,
        missing_since=missing_since,
        last_missing_at=last_missing_at,
        original_source_ref=original or None,
        encoded=True,
    )


def _encode_state(
    *,
    original_source_ref: str | None,
    status: str,
    missing_runs: int,
    missing_since: datetime,
    last_missing_at: datetime,
) -> str | None:
    payload = {
        "s": status,
        "r": missing_runs,
        "f": _json_datetime(missing_since),
        "a": _json_datetime(last_missing_at),
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    value = f"{SOURCE_REF_PREFIX}{status}:{token}:{original_source_ref or ''}"
    return value if len(value) <= SOURCE_REF_MAX_LENGTH else None


def _new_updated_at(current: Any, observed_at: datetime) -> datetime:
    if not isinstance(current, datetime):
        return observed_at
    return observed_at if _as_utc(current) <= observed_at else current


def _newer_timestamp(current: Any, observed_at: datetime) -> datetime:
    if not isinstance(current, datetime):
        return observed_at
    return observed_at if _as_utc(current) < observed_at else current


def _age_days(observed_at: datetime, last_seen_at: Any) -> float | None:
    if not isinstance(last_seen_at, datetime):
        return None
    seconds = max(0.0, (observed_at - _as_utc(last_seen_at)).total_seconds())
    return seconds / 86_400.0


def _decision(
    job: JobSnapshot,
    company: CompanyRunObservation,
    *,
    observed_at: datetime,
    grace_runs: int,
    grace_days: float,
) -> _JobDecision:
    job_id = _text(job.id)
    state = decode_offline_state(job.source_ref)
    last_seen_at = _as_utc(job.last_seen_at) if isinstance(job.last_seen_at, datetime) else None
    if not state.valid:
        plan = JobReconciliationPlan(
            job_id=job_id,
            company_id=_text(job.company_id),
            action=ReconciliationAction.SKIPPED.value,
            previous_status="unknown",
            status="unknown",
            missing_runs=0,
            missing_since=None,
            last_missing_at=None,
            last_seen_at=last_seen_at,
            age_days=_age_days(observed_at, job.last_seen_at),
            grace_runs_met=False,
            grace_days_met=False,
            persistable=False,
            will_write=False,
            reason="invalid_offline_source_ref",
        )
        return _JobDecision(plan, None, None, None)

    previous_status = state.status
    if job_id in company.observed_job_ids:
        new_source_ref = state.original_source_ref if state.encoded else job.source_ref
        new_last_seen_at = _newer_timestamp(job.last_seen_at, observed_at)
        needs_write = (
            new_source_ref != job.source_ref
            or new_last_seen_at != job.last_seen_at
        )
        action = (
            ReconciliationAction.RESTORED.value
            if previous_status != JobVisibility.ACTIVE.value
            else ReconciliationAction.OBSERVED.value
        )
        plan = JobReconciliationPlan(
            job_id=job_id,
            company_id=_text(job.company_id),
            action=action,
            previous_status=previous_status,
            status=JobVisibility.ACTIVE.value,
            missing_runs=0,
            missing_since=None,
            last_missing_at=None,
            last_seen_at=new_last_seen_at,
            age_days=0.0,
            grace_runs_met=False,
            grace_days_met=False,
            persistable=True,
            will_write=needs_write,
            reason="job_observed" if action == ReconciliationAction.OBSERVED.value else "job_restored",
        )
        return _JobDecision(
            plan,
            new_source_ref,
            new_last_seen_at,
            _new_updated_at(job.updated_at, observed_at) if needs_write else None,
        )

    duplicate = state.last_missing_at == observed_at
    missing_runs = state.missing_runs if duplicate else state.missing_runs + 1
    missing_since = state.missing_since or observed_at
    last_missing_at = state.last_missing_at if duplicate else observed_at
    age_days = _age_days(observed_at, job.last_seen_at)
    required_runs = max(2, grace_runs)
    runs_met = missing_runs >= required_runs
    days_met = age_days is not None and age_days >= grace_days
    should_inactivate = runs_met and days_met
    resulting_status = (
        JobVisibility.INACTIVE.value
        if previous_status == JobVisibility.INACTIVE.value or should_inactivate
        else JobVisibility.MISSING.value
    )
    if previous_status == JobVisibility.INACTIVE.value:
        action = ReconciliationAction.UNCHANGED.value if duplicate else ReconciliationAction.INACTIVE.value
    elif should_inactivate:
        action = ReconciliationAction.INACTIVE.value
    elif duplicate:
        action = ReconciliationAction.UNCHANGED.value
    else:
        action = ReconciliationAction.MISSING.value

    if resulting_status == JobVisibility.INACTIVE.value:
        reason = "grace_threshold_reached" if should_inactivate else "already_inactive"
    elif age_days is None:
        reason = "last_seen_at_missing"
    elif not runs_met:
        reason = "grace_runs_not_reached"
    else:
        reason = "grace_days_not_reached"
    new_source_ref = _encode_state(
        original_source_ref=state.original_source_ref if state.encoded else job.source_ref,
        status=resulting_status,
        missing_runs=missing_runs,
        missing_since=missing_since,
        last_missing_at=last_missing_at,
    )
    persistable = new_source_ref is not None
    needs_write = persistable and new_source_ref != job.source_ref
    if not persistable:
        reason = f"{reason};source_ref_capacity_exceeded"
    plan = JobReconciliationPlan(
        job_id=job_id,
        company_id=_text(job.company_id),
        action=action,
        previous_status=previous_status,
        status=resulting_status,
        missing_runs=missing_runs,
        missing_since=missing_since,
        last_missing_at=last_missing_at,
        last_seen_at=last_seen_at,
        age_days=age_days,
        grace_runs_met=runs_met,
        grace_days_met=days_met,
        persistable=persistable,
        will_write=needs_write,
        reason=reason,
    )
    return _JobDecision(
        plan,
        new_source_ref,
        None,
        _new_updated_at(job.updated_at, observed_at) if needs_write else None,
    )


def _apply_decision(job: JobSnapshot, decision: _JobDecision) -> None:
    if not decision.plan.will_write:
        return
    job.source_ref = decision.new_source_ref
    if decision.new_last_seen_at is not None:
        job.last_seen_at = decision.new_last_seen_at
    if decision.new_updated_at is not None:
        job.updated_at = decision.new_updated_at


def _extract_observed_at(
    explicit: Any,
    alternate: Any,
    run: Any,
) -> datetime:
    candidates = [explicit, alternate]
    if run is not None:
        candidates.extend(
            [
                _field(run, ("observed_at", "run_observed_at"), None),
                _field(run, ("finished_at", "completed_at"), None),
            ]
        )
        pipeline = _field(run, ("pipeline", "pipeline_result"), None)
        if pipeline is not None:
            candidates.extend(
                [
                    _field(pipeline, ("observed_at", "run_observed_at"), None),
                    _field(pipeline, ("finished_at", "completed_at"), None),
                ]
            )
    for candidate in candidates:
        if candidate is not None:
            result = _coerce_datetime(candidate, field_name="observed_at")
            if result is not None:
                return result
    raise ValueError("run observed_at is required for offline reconciliation")


def _result(
    *,
    observed_at: datetime,
    grace_runs: int,
    grace_days: float,
    dry_run: bool,
    written: bool,
    inputs: Sequence[_CompanyInput],
    skipped_company_ids: Sequence[str],
    plans: Sequence[JobReconciliationPlan],
) -> OfflineReconciliationResult:
    plan_tuple = tuple(plans)
    processed = tuple(sorted(item.observation.company_id for item in inputs if item.eligible))
    return OfflineReconciliationResult(
        observed_at=observed_at,
        grace_runs=grace_runs,
        grace_days=grace_days,
        dry_run=dry_run,
        written=written,
        processed_company_ids=processed,
        skipped_company_ids=tuple(sorted(set(skipped_company_ids))),
        plans=plan_tuple,
        observed_count=sum(item.action == ReconciliationAction.OBSERVED.value for item in plan_tuple),
        missing_count=sum(item.action == ReconciliationAction.MISSING.value for item in plan_tuple),
        inactive_count=sum(
            item.action == ReconciliationAction.INACTIVE.value
            and item.previous_status != JobVisibility.INACTIVE.value
            for item in plan_tuple
        ),
        restored_count=sum(item.action == ReconciliationAction.RESTORED.value for item in plan_tuple),
        unchanged_count=sum(item.action == ReconciliationAction.UNCHANGED.value for item in plan_tuple),
        skipped_count=sum(item.action == ReconciliationAction.SKIPPED.value for item in plan_tuple),
        planned_only_count=sum(
            item.action not in {ReconciliationAction.SKIPPED.value, ReconciliationAction.UNCHANGED.value}
            and not item.persistable
            for item in plan_tuple
        ),
    )


def reconcile_offline_jobs(
    storage: Storage,
    run: Any = None,
    *,
    observed_at: datetime | None = None,
    run_observed_at: datetime | None = None,
    company_runs: Any = None,
    successful_companies: Any = None,
    observed_jobs: Any = None,
    observed_job_ids: Any = None,
    pipeline_result: Any = None,
    grace_runs: int = DEFAULT_GRACE_RUNS,
    grace_days: float = DEFAULT_GRACE_DAYS,
    dry_run: bool = False,
) -> OfflineReconciliationResult:
    """Plan and optionally persist disappearance/recovery decisions.

    ``company_runs`` is normally a sequence of ``CompanyRunObservation`` or
    the ``company_results`` returned by the daily pipeline.  For callers that
    keep job observations separately, pass ``observed_job_ids`` or
    ``observed_jobs`` as ``company_id -> job ids``.  A company is reconciled
    only when its row is complete, successful, and has an explicit observation
    set.  Missing runs from failed or partial companies are ignored.
    """

    if not isinstance(storage, Storage):
        raise TypeError("storage must be a Storage instance")
    if isinstance(run, datetime) and observed_at is None and run_observed_at is None:
        observed_at = run
        run = None
    source_run = pipeline_result if pipeline_result is not None else run
    observed_timestamp = _extract_observed_at(observed_at, run_observed_at, source_run)
    if isinstance(grace_runs, bool) or not isinstance(grace_runs, int) or grace_runs < 1:
        raise ValueError("grace_runs must be a positive integer")
    if isinstance(grace_days, bool) or not isinstance(grace_days, (int, float)) or grace_days < 0:
        raise ValueError("grace_days must be a non-negative number")
    grace_days_value = float(grace_days)
    observed_index = _observed_index(observed_jobs)
    observed_index.update(_observed_index(observed_job_ids))
    inputs, skipped_company_ids = _company_inputs(
        run=source_run,
        company_runs=company_runs,
        successful_companies=successful_companies,
        observed=observed_index,
    )
    eligible_inputs = [item for item in inputs if item.eligible]
    if not eligible_inputs:
        return _result(
            observed_at=observed_timestamp,
            grace_runs=grace_runs,
            grace_days=grace_days_value,
            dry_run=dry_run,
            written=False,
            inputs=inputs,
            skipped_company_ids=skipped_company_ids,
            plans=(),
        )

    company_ids = [item.observation.company_id for item in eligible_inputs]
    observed_by_company = {item.observation.company_id: item.observation for item in eligible_inputs}

    def read_decisions(session: Any) -> tuple[list[JobSnapshot], list[_JobDecision]]:
        rows = list(
            session.scalars(
                select(JobSnapshot).where(JobSnapshot.company_id.in_(company_ids))
            ).all()
        )
        decisions: list[_JobDecision] = []
        for job in rows:
            company = observed_by_company.get(_text(job.company_id))
            if company is not None:
                decisions.append(
                    _decision(
                        job,
                        company,
                        observed_at=observed_timestamp,
                        grace_runs=grace_runs,
                        grace_days=grace_days_value,
                    )
                )
        return rows, decisions

    if dry_run:
        with storage.session() as session:
            _rows, decisions = read_decisions(session)
        return _result(
            observed_at=observed_timestamp,
            grace_runs=grace_runs,
            grace_days=grace_days_value,
            dry_run=True,
            written=False,
            inputs=inputs,
            skipped_company_ids=skipped_company_ids,
            plans=[item.plan for item in decisions],
        )

    with storage.session() as session:
        _rows, preview_decisions = read_decisions(session)
    if not any(item.plan.will_write for item in preview_decisions):
        return _result(
            observed_at=observed_timestamp,
            grace_runs=grace_runs,
            grace_days=grace_days_value,
            dry_run=False,
            written=False,
            inputs=inputs,
            skipped_company_ids=skipped_company_ids,
            plans=[item.plan for item in preview_decisions],
        )

    with storage.write_transaction() as session:
        rows, decisions = read_decisions(session)
        jobs_by_id = {_text(job.id): job for job in rows}
        for item in decisions:
            _apply_decision(jobs_by_id[item.plan.job_id], item)
    return _result(
        observed_at=observed_timestamp,
        grace_runs=grace_runs,
        grace_days=grace_days_value,
        dry_run=False,
        written=True,
        inputs=inputs,
        skipped_company_ids=skipped_company_ids,
        plans=[item.plan for item in decisions],
    )


def reconcile_missing_jobs(*args: Any, **kwargs: Any) -> OfflineReconciliationResult:
    """Compatibility spelling for callers focused on missing jobs."""

    return reconcile_offline_jobs(*args, **kwargs)


def run_offline_reconciliation(*args: Any, **kwargs: Any) -> OfflineReconciliationResult:
    """Compatibility spelling for scheduled pipeline adapters."""

    return reconcile_offline_jobs(*args, **kwargs)


reconcile_offline = reconcile_offline_jobs


__all__ = [
    "CompanyRunObservation",
    "DEFAULT_GRACE_DAYS",
    "DEFAULT_GRACE_RUNS",
    "JobReconciliationPlan",
    "JobVisibility",
    "OfflineJobState",
    "OfflineReconciliationResult",
    "ReconciliationAction",
    "SOURCE_REF_MAX_LENGTH",
    "SOURCE_REF_PREFIX",
    "INACTIVE_SOURCE_REF_PREFIX",
    "decode_offline_state",
    "reconcile_missing_jobs",
    "reconcile_offline",
    "reconcile_offline_jobs",
    "run_offline_reconciliation",
]
