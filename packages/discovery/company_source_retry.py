"""Bounded, read-only retry execution for one company source record."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from packages.discovery.company_registry import (
    CompanySourceNotFound,
    CompanySourceRegistry,
    validate_public_http_url,
)
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.recruitment_core.entry import diagnose_candidate_entry
from packages.discovery.oc_capture import classify_oc_destination_url
from packages.tools.crawler_audit import (
    CrawlerAcceptanceInput,
    ObservedCrawlerJob,
    accept_crawler_run,
)
from packages.tools.oc_candidates import SubprocessCandidateCrawlerProcess


class CompanySourceRetryConflict(RuntimeError):
    """The same record already has an active retry."""


class CompanySourceRetryCapacity(RuntimeError):
    """The bounded retry worker pool is full."""


class CompanySourceRetryRejected(ValueError):
    """The stored entry is not a safe, crawlable candidate."""


class CompanySourceRetryStartError(RuntimeError):
    """The worker thread could not be started."""


@dataclass(frozen=True)
class RetryStarted:
    record_id: str
    thread_name: str


class CompanySourceRetryService:
    """Run isolated candidate crawls and persist only source attempt outcomes."""

    def __init__(
        self,
        registry: CompanySourceRegistry,
        *,
        process: Callable[..., dict[str, Any] | list[dict[str, Any]]] | None = None,
        max_concurrency: int = 2,
        timeout_seconds: float = 75.0,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 2:
            raise ValueError("max_concurrency must be between 1 and 2")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry = registry
        self.process = process or SubprocessCandidateCrawlerProcess()
        self.max_concurrency = max_concurrency
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._active: dict[str, threading.Thread] = {}

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def start_retry(self, record_id: str) -> RetryStarted:
        record = self.registry.get_source(record_id)
        if record is None:
            raise CompanySourceNotFound(record_id)
        try:
            entry_url = validate_public_http_url(record["entry_url"])
        except ValueError as exc:
            self._record_rejection(record_id, str(exc), code="unsafe_entry_url")
            raise CompanySourceRetryRejected(str(exc)) from exc

        excluded = classify_oc_destination_url(entry_url)
        if excluded is not None:
            kind, reason = excluded
            self._record_rejection(record_id, reason, code=str(kind))
            raise CompanySourceRetryRejected(reason)
        diagnosis = diagnose_candidate_entry(entry_url)
        if diagnosis.entry_kind in {"invalid_entry", "form_application"} or not diagnosis.crawler_key:
            self._record_rejection(record_id, diagnosis.reason, code="invalid_entry")
            raise CompanySourceRetryRejected(diagnosis.reason)

        started = threading.Event()
        with self._lock:
            if record_id in self._active:
                raise CompanySourceRetryConflict(record_id)
            if len(self._active) >= self.max_concurrency:
                raise CompanySourceRetryCapacity("company source retry capacity is full")
            thread = threading.Thread(
                target=self._run,
                kwargs={
                    "record_id": record_id,
                    "company": record["company_name"],
                    "source_url": entry_url,
                    "crawler_key": diagnosis.crawler_key,
                    "started": started,
                },
                name=f"company-source-retry-{record_id[:12]}",
                daemon=True,
            )
            self._active[record_id] = thread
            try:
                # The API callback is the dispatch boundary. The marker is
                # written only while this record owns a worker slot.
                self.registry.record_attempt(record_id, status="running", attempted_url=entry_url)
                thread.start()
            except Exception as exc:
                self._active.pop(record_id, None)
                self._record_failure(record_id, "retry_start_failed", str(exc))
                raise CompanySourceRetryStartError("retry thread could not be started") from exc
        if not started.wait(timeout=5.0):
            self._record_failure(record_id, "retry_start_failed", "retry thread did not report started")
            raise CompanySourceRetryStartError("retry thread did not report started")
        return RetryStarted(record_id=record_id, thread_name=thread.name)

    def _run(
        self,
        *,
        record_id: str,
        company: str,
        source_url: str,
        crawler_key: str,
        started: threading.Event,
    ) -> None:
        started.set()
        try:
            result = self.process(
                company=company,
                crawler_key=crawler_key,
                source_url=source_url,
                timeout_seconds=self.timeout_seconds,
                source_context={
                    "read_only": True,
                    "company_source_retry": True,
                    "record_id": record_id,
                },
            )
            self._record_result(record_id, source_url, result)
        except Exception as exc:
            self._record_failure(record_id, "crawl", str(exc))
        finally:
            with self._lock:
                self._active.pop(record_id, None)

    def _record_result(
        self,
        record_id: str,
        source_url: str,
        result: dict[str, Any] | list[dict[str, Any]],
    ) -> None:
        if isinstance(result, list):
            # A list carries no page/count evidence. Treat it as unknown.
            payload: dict[str, Any] = {"jobs": result}
        elif isinstance(result, dict):
            payload = result
        else:
            self._record_failure(record_id, "crawl_output", "crawler returned invalid output")
            return
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            self._record_failure(record_id, "crawl_output", "crawler returned no jobs list")
            return

        observed_jobs = self._observed_jobs(jobs)
        has_more = _strict_bool(payload.get("has_more"))
        pagination_complete = _strict_bool(payload.get("pagination_complete"))
        completeness_known = _strict_bool(payload.get("completeness_known"))
        if has_more is None and "has_more" in payload:
            completeness_known = False
        if pagination_complete is None and "pagination_complete" in payload:
            completeness_known = False
        pages_seen = _nonnegative_int(payload.get("pages_seen"), default=0)
        total_pages = _nonnegative_int(payload.get("total_pages"))
        advertised_total = _nonnegative_int(
            payload.get("advertised_total", payload.get("total_count", payload.get("total")))
        )
        source_url = str(payload.get("source_url") or source_url)
        allowed_origins = [source_url, *(payload.get("allowed_origins") or [])]
        allowed_detail_urls = [str(item) for item in (payload.get("allowed_detail_urls") or [])]
        audit = accept_crawler_run(
            CrawlerAcceptanceInput(
                company=str(payload.get("company") or record_id),
                source_url=source_url,
                allowed_origins=allowed_origins,
                allowed_detail_urls=allowed_detail_urls,
                jobs=observed_jobs,
                pages_seen=pages_seen,
                total_pages=total_pages,
                has_more=has_more is True,
                pagination_complete=pagination_complete,
                completeness_known=completeness_known,
                advertised_total=advertised_total,
                require_confirmed_cohort=False,
                require_complete_jd=False,
                timeout_ms=int(self.timeout_seconds * 1_000),
            )
        )
        audit_data = audit.data
        accepted_jobs = audit_data.accepted_jobs if audit_data is not None else []
        jd_pending_count = sum(
            assess_jd_capture(job.model_dump(mode="python")).incomplete
            for job in accepted_jobs
        )
        job_count = len(accepted_jobs)
        audit_error = _error_value(getattr(audit, "error_code", None))
        process_error = str(payload.get("error_code") or "")
        error_code = process_error or audit_error
        error_message = str(payload.get("error_message") or getattr(audit, "error_message", "") or error_code)
        pagination_state = audit_data.pagination_state if audit_data is not None else "unknown"
        actual_pagination_complete = (
            True if pagination_state == "complete" else False if pagination_state == "incomplete" else None
        )
        if process_error:
            status = "unusable" if process_error in {
                "invalid_entry", "form_application_only", "login_required", "captcha_required",
                "access_denied",
            } else "failed"
            failure_stage = "diagnosis" if status == "unusable" else "crawl"
        elif pagination_state != "complete":
            status, failure_stage = ("partial" if job_count else "failed"), "pagination"
            error_code = error_code or (
                "pagination_incomplete" if pagination_state == "incomplete" else "pagination_evidence_missing"
            )
        elif audit_data is not None and audit_data.rejected_count and not audit.success:
            status, failure_stage = "failed", "list/detail"
        elif jd_pending_count:
            status, failure_stage = "partial", "detail"
            error_code = error_code or "jd_capture_incomplete"
            error_message = error_message or "One or more accepted jobs lack verified JD capture."
        else:
            status, failure_stage = "complete", ""
        self.registry.record_attempt(
            record_id,
            status=status,
            attempted_url=source_url,
            final_url=str(payload.get("final_url") or source_url),
            failure_stage=failure_stage,
            reason_code=error_code,
            reason=error_message,
            job_count=max(0, job_count),
            jd_pending_count=max(0, jd_pending_count),
            pagination_complete=actual_pagination_complete,
        )

    @staticmethod
    def _observed_jobs(jobs: list[Any]) -> list[ObservedCrawlerJob]:
        observed: list[ObservedCrawlerJob] = []
        for index, raw in enumerate(jobs):
            item = raw if isinstance(raw, dict) else {}
            observed.append(
                ObservedCrawlerJob.model_validate(
                    {
                        "id": str(item.get("id") or item.get("source_job_id") or item.get("job_id") or f"invalid-{index}"),
                        "title": str(item.get("title") or ""),
                        "city": item.get("city"),
                        "detail_url": str(item.get("detail_url") or item.get("jd_url") or ""),
                        "jd_raw": item.get("jd_raw"),
                        "capture_evidence": item.get("capture_evidence") or {},
                        "cohort": item.get("cohort"),
                        "cohort_status": str(item.get("cohort_status") or "unconfirmed"),
                        "cohort_source": item.get("cohort_source"),
                        "cohort_evidence": item.get("cohort_evidence"),
                        "batch": item.get("batch") or "unknown",
                    }
                )
            )
        return observed

    def _record_failure(self, record_id: str, stage: str, reason: str) -> None:
        try:
            self.registry.record_attempt(
                record_id,
                status="failed",
                failure_stage=stage,
                reason_code="retry_failed",
                reason=reason[-10_000:],
            )
        except CompanySourceNotFound:
            return

    def _record_rejection(self, record_id: str, reason: str, *, code: str) -> None:
        self.registry.record_attempt(
            record_id,
            status="unusable",
            failure_stage="diagnosis",
            reason_code=code,
            reason=reason,
        )


__all__ = [
    "CompanySourceRetryCapacity",
    "CompanySourceRetryConflict",
    "CompanySourceRetryRejected",
    "CompanySourceRetryService",
    "CompanySourceRetryStartError",
    "RetryStarted",
    "SubprocessCandidateCrawlerProcess",
    "diagnose_candidate_entry",
]


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _nonnegative_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _error_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))
