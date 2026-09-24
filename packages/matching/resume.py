from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
import time
from typing import Any

from sqlalchemy import select

from packages.domain.models import Job, JobAnalysis, RecruitmentBatch
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage
from packages.storage.sync import upsert_job_analysis_snapshot

from .models import AnalysisOutcome, AnalysisRecord, AnalysisStatus
from .title_policy import screen_title_job


QUOTA_ERROR_CODES = frozenset({"http_401", "http_402", "http_403"})
RETRYABLE_ERROR_CODES = frozenset({
    "structured_response_invalid",
    "transport_failed", "http_429",
})


@dataclass(frozen=True, slots=True)
class AnalysisResumeCandidate:
    job: Job
    payload: Mapping[str, Any]
    previous_status: str
    previous_error: str | None = None


@dataclass(frozen=True, slots=True)
class AnalysisResumePlan:
    total_jobs: int
    eligible_jobs: int
    completed_jobs: int
    pending_jobs: tuple[AnalysisResumeCandidate, ...]
    pending_by_previous: Mapping[str, int]

    @property
    def pending_count(self) -> int:
        return len(self.pending_jobs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_jobs": self.total_jobs,
            "eligible_jobs": self.eligible_jobs,
            "completed_jobs": self.completed_jobs,
            "pending_jobs": self.pending_count,
            "pending_by_previous": dict(self.pending_by_previous),
        }


@dataclass(slots=True)
class AnalysisResumeResult:
    planned: int
    processed: int = 0
    completed: int = 0
    failed: int = 0
    refused: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_reason: str | None = None
    errors: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "processed": self.processed,
            "completed": self.completed,
            "failed": self.failed,
            "refused": self.refused,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stopped_reason": self.stopped_reason,
            "errors": dict(self.errors),
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _job_from_row(row: JobSnapshot) -> Job:
    return Job(
        id=row.id,
        company_id=row.company_id,
        title=row.title,
        city=row.city,
        detail_url=row.detail_url,
        jd_raw=row.jd_raw,
        capture_evidence=row.capture_evidence or {},
        cohort=row.cohort,
        cohort_status=row.cohort_status,
        batch=RecruitmentBatch(row.batch),
        match_score=row.match_score,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        source=row.source,
        source_ref=row.source_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _analysis_from_record(record: AnalysisRecord) -> JobAnalysis:
    return JobAnalysis(
        match_score=record.match_score,
        advantages=list(record.advantages),
        gaps=list(record.gaps),
        summary=record.summary,
        recommendation=record.recommendation,
        score_breakdown=record.score_breakdown.model_dump(mode="json"),
        evidence=[item.model_dump(mode="json") for item in record.evidence],
        evidence_level=record.evidence_level.value if record.evidence_level else None,
        matched_directions=[item.value for item in record.matched_directions],
        primary_match_direction=(
            record.primary_match_direction.value if record.primary_match_direction else None
        ),
        analysis_status=record.analysis_status.value,
        model=record.model,
        analysis_version=record.analysis_version,
        prompt_version=record.prompt_version,
        content_fingerprint=record.content_fingerprint,
        profile_fingerprint=record.profile_fingerprint,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        filter_reasons=list(record.filter_reasons),
        refusal_reason=record.refusal_reason,
        error_code=record.error_code,
        analyzed_at=record.analyzed_at,
    )


def build_analysis_resume_plan(
    storage: Storage,
    profile: Mapping[str, Any],
    *,
    limit: int | None = None,
    company_ids: Sequence[str] = (),
) -> AnalysisResumePlan:
    """Select captured title-first jobs without a successful persisted score."""

    with storage.session() as session:
        statement = (
            select(JobSnapshot, JobAnalysisSnapshot, CompanySnapshot.name)
            .outerjoin(
                JobAnalysisSnapshot,
                JobAnalysisSnapshot.job_id == JobSnapshot.id,
            )
            .outerjoin(CompanySnapshot, CompanySnapshot.id == JobSnapshot.company_id)
        )
        scoped_ids = tuple(dict.fromkeys(str(value).strip() for value in company_ids if str(value).strip()))
        if scoped_ids:
            statement = statement.where(JobSnapshot.company_id.in_(scoped_ids))
        rows = session.execute(statement).all()

    candidates: list[AnalysisResumeCandidate] = []
    completed = 0
    eligible = 0
    pending_by_previous: Counter[str] = Counter()
    for row, analysis, company_name in rows:
        job = _job_from_row(row)
        payload = {
            **job.model_dump(mode="python"),
            "company": company_name or row.company_id,
        }
        if not screen_title_job(payload, profile).eligible:
            continue
        if not assess_jd_capture(payload).complete:
            continue
        eligible += 1
        if analysis is not None and analysis.analysis_status == AnalysisStatus.COMPLETE.value:
            completed += 1
            continue
        previous_status = analysis.analysis_status if analysis is not None else "missing"
        previous_error = analysis.error_code if analysis is not None else None
        pending_by_previous[
            f"{previous_status}:{previous_error or 'none'}"
        ] += 1
        candidates.append(
            AnalysisResumeCandidate(
                job=job,
                payload=payload,
                previous_status=previous_status or "missing",
                previous_error=previous_error,
            )
        )

    candidates.sort(
        key=lambda item: (
            0 if item.previous_status == AnalysisStatus.FAILED.value else 1,
            item.job.id,
        )
    )
    if limit is not None:
        candidates = candidates[: max(0, int(limit))]
    return AnalysisResumePlan(
        total_jobs=len(rows),
        eligible_jobs=eligible,
        completed_jobs=completed,
        pending_jobs=tuple(candidates),
        pending_by_previous=dict(sorted(pending_by_previous.items())),
    )


def _persist_wave(
    storage: Storage,
    items: Sequence[tuple[AnalysisResumeCandidate, AnalysisOutcome]],
) -> None:
    with storage.transaction(write=True) as session:
        for candidate, outcome in items:
            analysis = _analysis_from_record(outcome.result)
            upsert_job_analysis_snapshot(session, candidate.job, analysis)
            if outcome.result.analysis_status is AnalysisStatus.COMPLETE:
                row = session.get(JobSnapshot, candidate.job.id)
                if row is not None:
                    row.match_score = outcome.result.match_score
                    row.updated_at = _utc_now()


def resume_pending_analyses(
    storage: Storage,
    profile: Mapping[str, Any],
    service: Any,
    candidates: Sequence[AnalysisResumeCandidate],
    *,
    concurrency: int = 4,
    progress: Callable[[AnalysisResumeResult], None] | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    stop_requested: Event | None = None,
) -> AnalysisResumeResult:
    """Analyze pending candidates in bounded waves with provider-error circuit breaking."""

    worker_count = max(1, min(int(concurrency), 16))
    attempt_limit = max(1, min(int(max_attempts), 5))

    def analyze(candidate: AnalysisResumeCandidate) -> AnalysisOutcome:
        outcome: AnalysisOutcome | None = None
        for attempt in range(1, attempt_limit + 1):
            outcome = service.analyze_title_first(candidate.payload, profile)
            error = outcome.result.error_code or ""
            retryable = error in RETRYABLE_ERROR_CODES or error.startswith("http_5")
            if not retryable or attempt >= attempt_limit:
                return outcome
            if retry_backoff_seconds > 0:
                sleeper(min(retry_backoff_seconds * (2 ** (attempt - 1)), 8.0))
        assert outcome is not None
        return outcome

    result = AnalysisResumeResult(planned=len(candidates))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for offset in range(0, len(candidates), worker_count):
            if stop_requested is not None and stop_requested.is_set():
                result.stopped_reason = "time_budget_reached"
                break
            wave = tuple(candidates[offset : offset + worker_count])
            outcomes = list(
                executor.map(
                    analyze,
                    wave,
                )
            )
            persisted = list(zip(wave, outcomes, strict=True))
            _persist_wave(storage, persisted)
            for outcome in outcomes:
                record = outcome.result
                result.processed += 1
                result.input_tokens += record.input_tokens or 0
                result.output_tokens += record.output_tokens or 0
                if record.analysis_status is AnalysisStatus.COMPLETE:
                    result.completed += 1
                elif record.analysis_status is AnalysisStatus.REFUSED:
                    result.refused += 1
                else:
                    result.failed += 1
                    error = record.error_code or record.analysis_status.value
                    result.errors[error] += 1
                    if error in QUOTA_ERROR_CODES and result.stopped_reason is None:
                        result.stopped_reason = error
            if progress is not None:
                progress(result)
            if result.stopped_reason is not None:
                break
            if stop_requested is not None and stop_requested.is_set():
                result.stopped_reason = "time_budget_reached"
                break
    return result


__all__ = [
    "AnalysisResumeCandidate",
    "AnalysisResumePlan",
    "AnalysisResumeResult",
    "QUOTA_ERROR_CODES",
    "RETRYABLE_ERROR_CODES",
    "build_analysis_resume_plan",
    "resume_pending_analyses",
]
