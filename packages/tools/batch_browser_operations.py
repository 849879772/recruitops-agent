"""Batch Edge application-status review with verified database updates."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from packages.browser_bridge import BrowserBridgeStore
from packages.browser_bridge.models import OperationStatus
from packages.domain.urls import normalize_http_page_url
from packages.repositories.base import RecruitmentRepository
from packages.storage import Storage
from packages.tools.application_status_evidence import has_unmapped_status
from packages.tools.browser_bridge import (
    ObserveApplicationStatusPageInput,
    observe_application_status_page_workflow,
)
from packages.tools.browser_status_update import (
    BrowserStatusUpdateInput,
    UpdateStatus,
    browser_status_update,
)
from packages.tools.typed import (
    EvidenceSource,
    ToolErrorCode,
    ToolInput,
    ToolModel,
    ToolResponse,
    ToolStatus,
)


class BatchObserveApplicationStatusInput(ToolInput):
    """Applications to observe, bind to evidence, and safely reconcile."""

    application_ids: list[str] = Field(min_length=1, max_length=50)
    timeout_per_application_ms: int = Field(default=45_000, ge=5_000, le=120_000)
    max_concurrent: int = Field(default=3, ge=1, le=10)
    include_vision: Literal[False] = Field(
        default=False,
        description=(
            "Batch status review is DOM-only and never starts vision analysis. Use the separate "
            "individual observation flow for a justified single-page visual follow-up."
        ),
    )
    skip_on_captcha: bool = True
    skip_on_login: bool = True

    @field_validator("application_ids", mode="before")
    @classmethod
    def normalize_application_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("application_ids must be a list")
        normalized: list[str] = []
        for item in value:
            if isinstance(item, bool) or item is None:
                raise ValueError("application_ids must contain strings or integers")
            application_id = str(item).strip()
            if not application_id or len(application_id) > 255:
                raise ValueError("application_ids contains an invalid id")
            if application_id not in normalized:
                normalized.append(application_id)
        return normalized


class ApplicationStatusResult(ToolModel):
    """One application's truthful reconciliation outcome."""

    application_id: str
    state: Literal["updated", "unchanged", "blocked", "unresolved", "failed"]
    reason: str | None = None
    observed_status: str | None = None
    wrote: bool = False
    operation_id: str | None = None
    observation: dict[str, Any] | None = None
    elapsed_ms: int = Field(ge=0)


class BatchObserveApplicationStatusResponse(ToolResponse[dict[str, Any]]):
    """Batch result; success means every target was conclusively reconciled."""

    total: int = Field(ge=0)
    pages_total: int = Field(ge=0)
    updated: list[ApplicationStatusResult] = Field(default_factory=list)
    unchanged: list[ApplicationStatusResult] = Field(default_factory=list)
    blocked: list[ApplicationStatusResult] = Field(default_factory=list)
    unresolved: list[ApplicationStatusResult] = Field(default_factory=list)
    failed: list[ApplicationStatusResult] = Field(default_factory=list)
    # Compatibility projections for older UI/script clients.
    succeeded: list[ApplicationStatusResult] = Field(default_factory=list)
    skipped: list[ApplicationStatusResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    read_only: Literal[False] = False


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1_000))


def _value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _storage(repository: RecruitmentRepository) -> Storage | None:
    candidate = getattr(repository, "storage", None)
    return candidate if isinstance(candidate, Storage) else None


def _compact_observation(observation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(observation, dict):
        return None
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    vision = observation.get("vision") if isinstance(observation.get("vision"), dict) else {}
    return {
        "page": {
            "url": page.get("url"),
            "title": page.get("title"),
            "text": str(page.get("text") or "")[:4_000],
        },
        "application_records": list(observation.get("application_records") or [])[:50],
        "semantic_nodes": list(observation.get("semantic_nodes") or [])[:80],
        "vision": {
            "text": str(vision.get("text") or "")[:4_000],
            "confidence": vision.get("confidence"),
            "model": vision.get("model"),
            "image_sha256": vision.get("image_sha256"),
            "usage": vision.get("usage"),
        } if vision else None,
        "diagnostics": observation.get("diagnostics") or {},
    }


def _pause_reason(response: object) -> str | None:
    data = getattr(response, "data", None)
    result = getattr(data, "result", None)
    if isinstance(result, dict) and isinstance(result.get("pause"), dict):
        return str(result["pause"].get("reason") or "") or None
    return None


def _page_authentication_gate(observation: dict[str, Any]) -> bool:
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    text = str(page.get("text") or "").casefold()
    identity_prompt = any(token in text for token in (
        "请进行身份认证",
        "身份验证",
        "使用邮箱验证",
        "发送验证码",
        "verify your identity",
        "verification code",
    ))
    credential_prompt = any(token in text for token in (
        "手机号",
        "手机号码",
        "邮箱验证",
        "验证码",
        "phone number",
        "email verification",
    ))
    if identity_prompt and credential_prompt:
        return True
    # A navigation login link alone is not a gate when application cards loaded.
    has_records = bool(observation.get("application_records") or observation.get("entries"))
    login_shell = any(token in text for token in (
        "登录/注册", "登录 / 注册", "请先登录", "登录后查看投递", "请登录后查看",
    ))
    return login_shell and not has_records


def _application_page_unavailable(observation: dict[str, Any]) -> bool:
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    text = str(page.get("text") or "").casefold()
    if any(token in text for token in ("页面不存在", "页面已失效", "page not found", "404 not found")):
        return True
    # ATS bundles preload error illustrations even on working application pages.
    # Network resource names do not establish the visible page state.
    return False


def _identity_key(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _matching_record_only(
    application: object,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return one exact target card that contains no newer status evidence."""

    target = _identity_key(getattr(application, "job_title", ""))
    if not target:
        return None
    matches = [record for record in records if _identity_key(record.get("title")) == target]
    if len(matches) != 1:
        return None
    record = matches[0]
    signals = record.get("signals") if isinstance(record.get("signals"), dict) else {}
    if record.get("status") or has_unmapped_status(record) or signals.get("conflicting_statuses"):
        return None
    current_stage = _value(getattr(application, "stage", "")).casefold()
    return record if current_stage == "applied" else None


def _result(
    application_id: str,
    state: Literal["updated", "unchanged", "blocked", "unresolved", "failed"],
    *,
    started: float,
    reason: str | None = None,
    observed_status: str | None = None,
    wrote: bool = False,
    operation_id: str | None = None,
    observation: dict[str, Any] | None = None,
) -> ApplicationStatusResult:
    return ApplicationStatusResult(
        application_id=application_id,
        state=state,
        reason=reason,
        observed_status=observed_status,
        wrote=wrote,
        operation_id=operation_id,
        observation=observation,
        elapsed_ms=_elapsed_ms(started),
    )


def _error_response(
    request: BatchObserveApplicationStatusInput,
    *,
    started: float,
    reason: str,
    error_code: ToolErrorCode,
) -> BatchObserveApplicationStatusResponse:
    failures = [
        _result(item, "failed", started=started, reason=reason)
        for item in request.application_ids
    ]
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status",
        status=ToolStatus.FAILURE,
        success=False,
        total=len(request.application_ids),
        pages_total=0,
        failed=failures,
        data={"write_count": 0},
        summary={"total": len(request.application_ids), "failed": len(failures), "write_count": 0},
        evidence=[EvidenceSource(source="agent.application_snapshot")],
        error_code=error_code,
        error_message=reason,
        timeout_ms=request.timeout_per_application_ms,
        timed_out=error_code == ToolErrorCode.TIMEOUT,
        elapsed_ms=_elapsed_ms(started),
        read_only=False,
    )


async def batch_observe_application_status(
    request: BatchObserveApplicationStatusInput,
    browser_bridge_store: BrowserBridgeStore | None,
    repository: RecruitmentRepository,
) -> BatchObserveApplicationStatusResponse:
    """Observe each unique status page once and reconcile every bound application."""

    started = perf_counter()
    if not isinstance(request, BatchObserveApplicationStatusInput):
        request = BatchObserveApplicationStatusInput.model_validate(request)
    if browser_bridge_store is None:
        return _error_response(
            request,
            started=started,
            reason="The local Edge bridge is unavailable.",
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
        )
    storage = _storage(repository)
    if storage is None:
        return _error_response(
            request,
            started=started,
            reason=(
                "The repository does not expose the application storage required for audited writes."
            ),
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
        )

    try:
        applications = {str(item.id): item for item in repository.list_applications()}
    except Exception as exc:
        return _error_response(
            request,
            started=started,
            reason=str(exc) or "Applications could not be read.",
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
        )

    immediate: list[ApplicationStatusResult] = []
    groups: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    raw_urls: dict[str, str] = {}
    for application_id in dict.fromkeys(request.application_ids):
        item_started = perf_counter()
        application = applications.get(application_id)
        if application is None:
            immediate.append(_result(
                application_id, "failed", started=item_started, reason="application_not_found"
            ))
            continue
        normalized_url = normalize_http_page_url(application.record_url or "")
        if normalized_url is None:
            immediate.append(_result(
                application_id,
                "unresolved",
                started=item_started,
                reason="record_url_missing_or_invalid",
            ))
            continue
        groups[normalized_url].append((application_id, application))
        raw_urls.setdefault(normalized_url, application.record_url)

    semaphore = asyncio.Semaphore(request.max_concurrent)
    origin_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_group(
        normalized_url: str,
        members: list[tuple[str, Any]],
    ) -> list[ApplicationStatusResult]:
        group_started = perf_counter()
        application_ids = [item[0] for item in members]
        origin = urlparse(normalized_url).netloc.casefold()
        async with semaphore, origin_locks[origin]:
            observation_request = ObserveApplicationStatusPageInput(
                application_id=application_ids[0],
                application_ids=application_ids,
                application_url=raw_urls[normalized_url],
                timeout_ms=request.timeout_per_application_ms,
                include_vision=False,
                retain_on_pause=False,
                idempotency_key=(
                    f"batch-status-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-"
                    f"{application_ids[0]}"
                ),
            )
            try:
                observed = await asyncio.wait_for(
                    observe_application_status_page_workflow(
                        observation_request,
                        browser_bridge_store,
                        repository,
                    ),
                    timeout=request.timeout_per_application_ms / 1_000 + 5,
                )
            except asyncio.TimeoutError:
                return [
                    _result(item, "failed", started=group_started, reason="observation_timeout")
                    for item in application_ids
                ]
            except Exception as exc:
                return [
                    _result(
                        item,
                        "failed",
                        started=group_started,
                        reason=f"{type(exc).__name__}: {str(exc)}"[:200],
                    )
                    for item in application_ids
                ]

        data = getattr(observed, "data", None)
        operation_id = getattr(data, "operation_id", None)
        error_code = _value(
            getattr(data, "error_code", None) or getattr(observed, "error_code", None)
        )
        operation_status = getattr(data, "status", None)
        pause_reason = _pause_reason(observed)

        if error_code == "CAPTCHA_REQUIRED" or pause_reason == "captcha_required":
            state = "blocked" if request.skip_on_captcha else "failed"
            return [
                _result(
                    item,
                    state,
                    started=group_started,
                    reason="captcha_required",
                    operation_id=operation_id,
                )
                for item in application_ids
            ]
        if (
            error_code == "WAITING_FOR_LOGIN"
            or operation_status == OperationStatus.WAITING_FOR_LOGIN
            or pause_reason == "login_required"
        ):
            state = "blocked" if request.skip_on_login else "failed"
            return [
                _result(
                    item,
                    state,
                    started=group_started,
                    reason="login_required",
                    operation_id=operation_id,
                )
                for item in application_ids
            ]

        observation = getattr(data, "observation", None)
        if not isinstance(observation, dict):
            return [
                _result(
                    item,
                    "unresolved" if error_code in {"STATE_UNCLEAR", ""} else "failed",
                    started=group_started,
                    reason=(pause_reason or error_code or "status_evidence_missing").casefold(),
                    operation_id=operation_id,
                )
                for item in application_ids
            ]
        if _page_authentication_gate(observation):
            state = "blocked" if request.skip_on_login else "failed"
            return [
                _result(
                    item,
                    state,
                    started=group_started,
                    reason="authentication_required",
                    operation_id=operation_id,
                )
                for item in application_ids
            ]
        if _application_page_unavailable(observation):
            compact = _compact_observation(observation)
            return [
                _result(
                    item,
                    "unresolved",
                    started=group_started,
                    reason="application_page_unavailable",
                    operation_id=operation_id,
                    observation=compact,
                )
                for item in application_ids
            ]
        entries = observation.get("entries")
        records = [
            record
            for record in (observation.get("application_records") or [])
            if isinstance(record, dict)
        ]
        if not isinstance(entries, list) or not entries:
            conflict = any(
                isinstance(record, dict) and record.get("signals", {}).get("conflicting_statuses")
                for record in records
            )
            reason = "status_evidence_conflict" if conflict else "status_evidence_missing"
            compact = _compact_observation(observation)
            results: list[ApplicationStatusResult] = []
            applications_by_id = {item[0]: item[1] for item in members}
            for item in application_ids:
                if _matching_record_only(applications_by_id[item], records):
                    results.append(_result(
                        item,
                        "unchanged",
                        started=group_started,
                        reason="no_newer_status_observed",
                        observed_status="applied",
                        operation_id=operation_id,
                    ))
                else:
                    results.append(_result(
                        item,
                        "unresolved",
                        started=group_started,
                        reason=("status_unmapped" if any(
                            _identity_key(record.get("title")) == _identity_key(getattr(applications_by_id[item], "job_title", ""))
                            and has_unmapped_status(record) for record in records
                        ) else reason),
                        operation_id=operation_id,
                        observation=compact,
                    ))
            return results

        terminal_result = {
            "operation_id": operation_id,
            "operation_status": "SUCCEEDED",
            "status": "multiple" if len(entries) > 1 else "",
            "entries": entries,
            "captured_at": observation.get("captured_at") or datetime.now(timezone.utc).isoformat(),
        }
        results: list[ApplicationStatusResult] = []
        applications_by_id = {item[0]: item[1] for item in members}
        for application_id in application_ids:
            if any(
                _identity_key(record.get("title")) == _identity_key(getattr(applications_by_id[application_id], "job_title", ""))
                and has_unmapped_status(record) for record in records
            ):
                results.append(_result(
                    application_id, "unresolved", started=group_started, reason="status_unmapped",
                    operation_id=operation_id, observation=_compact_observation(observation),
                ))
                continue
            if _matching_record_only(applications_by_id[application_id], records):
                results.append(_result(
                    application_id,
                    "unchanged",
                    started=group_started,
                    reason="no_newer_status_observed",
                    observed_status="applied",
                    operation_id=operation_id,
                ))
                continue
            update = browser_status_update(
                BrowserStatusUpdateInput(
                    application_id=application_id,
                    page_url=raw_urls[normalized_url],
                    terminal_result=terminal_result,
                    operation_id=operation_id,
                ),
                storage,
            )
            update_data = update.data
            observed_status = _value(getattr(update_data, "observed_status", None)) or None
            reason = getattr(update_data, "reason_code", None) or update.error_code
            if update.status is UpdateStatus.UPDATED:
                state = "updated"
            elif update.status is UpdateStatus.UNCHANGED:
                state = "unchanged"
            elif update.status in {UpdateStatus.APPROVAL_REQUIRED, UpdateStatus.STATE_UNCLEAR}:
                state = "unresolved"
            else:
                state = "failed"
            results.append(_result(
                application_id,
                state,
                started=group_started,
                reason=reason,
                observed_status=observed_status,
                wrote=bool(getattr(update_data, "wrote", False)),
                operation_id=operation_id,
                observation=_compact_observation(observation) if state == "unresolved" else None,
            ))
        return results

    grouped_results = await asyncio.gather(
        *(process_group(url, members) for url, members in groups.items()),
        return_exceptions=False,
    )
    results = immediate + [item for group in grouped_results for item in group]
    buckets = {
        state: [item for item in results if item.state == state]
        for state in ("updated", "unchanged", "blocked", "unresolved", "failed")
    }
    reconciled = len(buckets["updated"]) + len(buckets["unchanged"])
    write_count = sum(item.wrote for item in buckets["updated"])
    all_reconciled = len(results) == len(request.application_ids) and reconciled == len(results)
    all_failed = bool(results) and len(buckets["failed"]) == len(results)
    status = (
        ToolStatus.SUCCESS
        if all_reconciled
        else (ToolStatus.FAILURE if all_failed else ToolStatus.AMBIGUOUS)
    )
    error_code = None if status is ToolStatus.SUCCESS else (
        ToolErrorCode.SOURCE_UNAVAILABLE
        if status is ToolStatus.FAILURE
        else ToolErrorCode.AMBIGUOUS_MATCH
    )
    summary = {
        "total": len(request.application_ids),
        "pages_total": len(groups),
        "updated": len(buckets["updated"]),
        "unchanged": len(buckets["unchanged"]),
        "blocked": len(buckets["blocked"]),
        "unresolved": len(buckets["unresolved"]),
        "failed": len(buckets["failed"]),
        "write_count": write_count,
        "completion_rate": round(reconciled / len(request.application_ids), 4),
        "success_means": "every requested application was verified as updated or unchanged",
    }
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status",
        status=status,
        success=status is ToolStatus.SUCCESS,
        total=len(request.application_ids),
        pages_total=len(groups),
        updated=buckets["updated"],
        unchanged=buckets["unchanged"],
        blocked=buckets["blocked"],
        unresolved=buckets["unresolved"],
        failed=buckets["failed"],
        succeeded=buckets["updated"] + buckets["unchanged"],
        skipped=buckets["blocked"] + buckets["unresolved"],
        summary=summary,
        data=summary,
        evidence=[
            EvidenceSource(source="edge.browser_operation"),
            EvidenceSource(source="agent.application_snapshot"),
            EvidenceSource(source="agent.write_audit"),
        ],
        error_code=error_code,
        error_message=(
            None if status is ToolStatus.SUCCESS
            else "One or more applications could not be conclusively reconciled."
        ),
        timeout_ms=request.timeout_per_application_ms,
        timed_out=False,
        elapsed_ms=_elapsed_ms(started),
        read_only=False,
    )


__all__ = [
    "ApplicationStatusResult",
    "BatchObserveApplicationStatusInput",
    "BatchObserveApplicationStatusResponse",
    "batch_observe_application_status",
]
