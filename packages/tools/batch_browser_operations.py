"""Batch Edge application-status review with verified database updates."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from packages.browser_bridge import BrowserBridgeStore
from packages.browser_bridge.models import OperationStatus
from packages.domain.urls import application_progress_channel, normalize_http_page_url
from packages.domain.application_identity import matching_records, unique_record
from packages.domain.application_status_semantics import current_status_labels, explicit_label_status, status_is_unasserted, timeline_without_current, status_evidence_conflict
from packages.repositories.base import RecruitmentRepository
from packages.storage import Storage
from packages.storage.application_reviews import safe_review_navigation_diagnostics
from packages.tools.application_status_evidence import has_unmapped_status, supports_no_newer_status
from packages.tools.application_review_summary import (
    review_presentation_summary, review_reason_breakdown, review_result_presentation,
)
from packages.tools.browser_bridge import (
    ObserveApplicationStatusPageInput,
    observe_application_status_page_workflow,
)
from packages.tools.browser_status_update import (
    BrowserStatusUpdateInput,
    UpdateStatus,
    _AUTO_CONFIDENCE,
    _normalise_status,
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


_TRANSIENT_RETRY_DELAY_SECONDS = 1.5


class BatchObserveApplicationStatusInput(ToolInput):
    """Applications to observe, bind to evidence, and safely reconcile."""

    application_ids: list[str] = Field(default_factory=list, max_length=50)
    run_id: str | None = Field(default=None, pattern=r"^status-review-[0-9a-f]{32}$")
    background: bool = Field(default=False, description="Legacy compatibility flag; reviews always await one bounded foreground wave.")
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    all_non_terminal: bool = Field(
        default=False,
        description=(
            "Select every persisted application except rejected and withdrawn records. "
            "Use this for a complete current-status review instead of querying full application "
            "records merely to collect their IDs."
        ),
    )
    timeout_per_application_ms: int = Field(default=45_000, ge=5_000, le=120_000)
    max_concurrent: int = Field(default=3, ge=1, le=10)
    include_vision: bool = Field(
        default=True,
        description=(
            "Use a bounded screenshot review directly when a readable page cannot be safely "
            "resolved by rules. Omit this field or use true for normal "
            "batch reviews; the service already starts with DOM-only. Use false only when the user "
            "explicitly declines image upload, not merely to choose the initial observation mode."
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

    @model_validator(mode="after")
    def validate_selection_mode(self) -> "BatchObserveApplicationStatusInput":
        if self.run_id:
            if self.all_non_terminal or self.application_ids:
                raise ValueError("run_id cannot be combined with a new selection")
            return self
        if self.all_non_terminal and self.application_ids:
            raise ValueError(
                "application_ids must be empty when all_non_terminal is enabled"
            )
        if not self.all_non_terminal and not self.application_ids:
            raise ValueError(
                "provide application_ids or enable all_non_terminal"
            )
        if self.background and not (self.all_non_terminal or self.run_id):
            raise ValueError("background requires a full review or an existing run_id")
        return self


class ApplicationStatusResult(ToolModel):
    """One application's truthful reconciliation outcome."""

    application_id: str
    company_name: str | None = None
    job_title: str | None = None
    state: Literal["updated", "unchanged", "excluded", "blocked", "unresolved", "failed"]
    reason: str | None = None
    observed_status: str | None = None
    observed_label: str | None = Field(default=None, max_length=200)
    presentation_state: Literal["retained"] | None = None
    saved_stage: str | None = None
    wrote: bool = False
    operation_id: str | None = None
    observation: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None
    model_disposition: str | None = None
    model_digest: str | None = None
    model_event_id: str | None = None
    vision_disposition: str | None = None
    elapsed_ms: int = Field(ge=0)
    checked_at: datetime | None = None


class BatchObserveApplicationStatusResponse(ToolResponse[dict[str, Any]]):
    """Batch result; success means every target was conclusively reconciled."""

    total: int = Field(ge=0)
    pages_total: int = Field(ge=0)
    updated: list[ApplicationStatusResult] = Field(default_factory=list)
    unchanged: list[ApplicationStatusResult] = Field(default_factory=list)
    excluded: list[ApplicationStatusResult] = Field(default_factory=list)
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
    diagnostics = dict(observation.get("diagnostics") or {})
    navigation = safe_review_navigation_diagnostics(observation.get("navigation_diagnostics")
                                                    or diagnostics.get("navigation_diagnostics"))
    if navigation:
        diagnostics["navigation_diagnostics"] = navigation
    return {
        "page": {
            "url": page.get("url") or page.get("page_url"),
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
        "diagnostics": diagnostics,
    }


def _pause_reason(response: object) -> str | None:
    data = getattr(response, "data", None)
    result = getattr(data, "result", None)
    if isinstance(result, dict) and isinstance(result.get("pause"), dict):
        return str(result["pause"].get("reason") or "") or None
    return None


def _page_authentication_gate(observation: dict[str, Any]) -> bool:
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    text = str(page.get("text") or "").casefold()[:800]
    identity_prompt = bool(re.search(
        r"请(?:先)?(?:进行|完成)?身份(?:认证|验证)|verify your identity", text,
    ))
    credential_prompt = any(token in text for token in (
        "手机号",
        "手机号码",
        "邮箱验证",
        "验证码",
        "phone number",
        "email verification",
    ))
    verification_prompt = bool(re.search(
        r"(?:发送|获取)(?:短信|邮箱|邮件)?验证码|(?:使用|通过)(?:手机|短信|邮箱|邮件)验证|verification code", text,
    ))
    if identity_prompt and credential_prompt and verification_prompt:
        return True
    # A public homepage can offer login/register before any record extraction.
    # Require an explicit access prompt or an active code-login form's wording.
    explicit_login = bool(re.search(
        r"请(?:先)?登录|登录(?:后|才能)(?:查看|访问|继续)|登录失效|重新登录|未登录|"
        r"login required|session expired|sign in to|log in to", text,
    ))
    code_login = (bool(re.search(r"(?:短信|手机|邮箱|邮件|验证码)(?:快捷)?登录", text))
                  and credential_prompt and verification_prompt) or (
        bool(re.search(r"登录|sign\s*in|log\s*in", text))
        and credential_prompt and verification_prompt
    )
    return explicit_login or code_login


def _application_page_unavailable(observation: dict[str, Any]) -> bool:
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    text = str(page.get("text") or "").casefold()
    if any(token in text for token in ("页面不存在", "页面已失效", "page not found", "404 not found")):
        return True
    # ATS bundles preload error illustrations even on working application pages.
    # Network resource names do not establish the visible page state.
    return False


def _origin_concurrency_key(normalized_url: str) -> str:
    """Serialize tenants that share one ATS backend and throttling boundary."""

    hostname = (urlparse(normalized_url).hostname or "").casefold()
    if hostname.endswith(".jobs.feishu.cn"):
        return "ats:feishu"
    return hostname


def _transient_empty_feishu_observation(
    observation: object,
    normalized_url: str,
) -> bool:
    """Detect an authenticated Feishu shell whose application list has not loaded."""

    hostname = (urlparse(normalized_url).hostname or "").casefold()
    if not hostname.endswith(".jobs.feishu.cn") or not isinstance(observation, dict):
        return False
    if observation.get("application_records") or observation.get("entries"):
        return False
    if _page_authentication_gate(observation):
        return False
    page = observation.get("page") if isinstance(observation.get("page"), dict) else {}
    text = str(page.get("text") or "").strip()
    return "/position/application" in normalized_url and len(text) < 80


def _matching_record_only(
    application: object,
    records: list[dict[str, Any]],
    page_applications: list | None = None,
) -> dict[str, Any] | None:
    """Return one exact target card that contains no decisive newer status."""

    record = unique_record(application, records)
    if record is None:
        return None
    if page_applications is not None:
        owners = [app for app in page_applications if any(card is record for card in matching_records(app, records))]
        if len(owners) != 1 or str(owners[0].id) != str(application.id):
            return None
    if timeline_without_current(record) or not current_status_labels(record):
        return None
    if status_evidence_conflict(record):
        return None
    record_status = _normalise_status(record.get("status"))
    if record_status not in {None, "applied"}:
        return None
    if record_status == "applied" and any(
        status_is_unasserted(label, "applied") or explicit_label_status(label) != "applied"
        for label in current_status_labels(record)
    ):
        return None
    if record_status is None and not supports_no_newer_status(record):
        return None
    current_stage = _value(getattr(application, "stage", "")).casefold()
    return record if current_stage and current_stage != "interested" else None


def _result(
    application_id: str,
    state: Literal["updated", "unchanged", "excluded", "blocked", "unresolved", "failed"],
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
        diagnostics=observation.get("diagnostics") if isinstance(observation, dict) else None,
        elapsed_ms=_elapsed_ms(started),
    )


def _error_response(
    request: BatchObserveApplicationStatusInput,
    *,
    started: float,
    reason: str,
    error_code: ToolErrorCode,
    application_ids: list[str] | None = None,
    excluded: list[ApplicationStatusResult] | None = None,
) -> BatchObserveApplicationStatusResponse:
    target_ids = request.application_ids if application_ids is None else application_ids
    excluded = excluded or []
    excluded_ids = {item.application_id for item in excluded}
    failures = [
        _result(item, "failed", started=started, reason=reason)
        for item in target_ids if item not in excluded_ids
    ]
    summary = {"total": len(target_ids), "failed": len(failures), "write_count": 0,
               "excluded": len(excluded),
               "excluded_mail_only": sum(item.reason == "mail_only" for item in excluded),
               "reason_breakdown": review_reason_breakdown(failures + excluded)}
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status",
        status=ToolStatus.FAILURE,
        success=False,
        total=len(target_ids),
        pages_total=0,
        failed=failures,
        excluded=excluded,
        skipped=excluded,
        data=summary,
        summary=summary,
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
    if request.all_non_terminal or request.run_id:
        from .application_review_run import continue_application_review

        return await continue_application_review(request, browser_bridge_store, repository)
    from .application_review_tasks import REVIEW_CONTEXT, review_dispatch_allowed
    from packages.config import get_settings
    if (isinstance(browser_bridge_store, BrowserBridgeStore) and REVIEW_CONTEXT.get() is None
            and get_settings().write_enabled):
        # Explicit retries need the same per-page checkpoint as full reviews.
        from .application_review_run import continue_application_review
        return await continue_application_review(request, browser_bridge_store, repository)
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
        application_rows = list(repository.list_applications())
        from .application_identity_binding import hydrate_verified_identity_bindings
        application_rows = hydrate_verified_identity_bindings(storage, application_rows)
        applications = {str(item.id): item for item in application_rows}
    except Exception as exc:
        return _error_response(
            request,
            started=started,
            reason=str(exc) or "Applications could not be read.",
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
        )

    selected_application_ids = list(request.application_ids)

    immediate: list[ApplicationStatusResult] = []
    groups: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    raw_urls: dict[str, str] = {}
    for application_id in dict.fromkeys(selected_application_ids):
        item_started = perf_counter()
        application = applications.get(application_id)
        if application is None:
            immediate.append(_result(
                application_id, "failed", started=item_started, reason="application_not_found"
            ))
            continue
        if _value(getattr(application, "stage", "")).casefold() in {"rejected", "withdrawn"}:
            immediate.append(_result(
                application_id,
                "excluded",
                started=item_started,
                reason="terminal_stage_excluded",
            ))
            continue
        if application_progress_channel(application.record_url) == "mail_only":
            immediate.append(_result(
                application_id,
                "excluded",
                started=item_started,
                reason="mail_only",
            ))
            continue
        normalized_url = normalize_http_page_url(application.record_url)
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
        page_applications = [app for app in application_rows if normalize_http_page_url(app.record_url or "") == normalized_url]
        if browser_bridge_store is None:
            return [
                _result(item, "failed", started=group_started,
                        reason="The local Edge bridge is unavailable.")
                for item in application_ids
            ]
        concurrency_key = _origin_concurrency_key(normalized_url)
        async with semaphore, origin_locks[concurrency_key]:
            if not review_dispatch_allowed():
                return [_result(item, "failed", started=group_started,
                                reason="review_control_requested") for item in application_ids]
            observation_request = None
            async def observe_once(attempt: int):
                nonlocal observation_request
                from .application_review_tasks import REVIEW_CONTEXT
                review_owner = REVIEW_CONTEXT.get()
                observation_request = ObserveApplicationStatusPageInput(
                    application_id=application_ids[0],
                    application_ids=application_ids,
                    application_url=raw_urls[normalized_url],
                    timeout_ms=request.timeout_per_application_ms,
                    include_vision=False,
                    retain_on_pause=False,
                    task_id=review_owner[1] if review_owner else None,
                    idempotency_key=(
                        f"batch-status-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-"
                        f"{application_ids[0]}-{attempt}"
                    ),
                )
                return await asyncio.wait_for(
                    observe_application_status_page_workflow(
                        observation_request,
                        browser_bridge_store,
                        repository,
                    ),
                    timeout=request.timeout_per_application_ms / 1_000 + 5,
                )

            try:
                observed = await observe_once(0)
                first_data = getattr(observed, "data", None)
                first_observation = getattr(first_data, "observation", None)
                first_error = _value(
                    getattr(first_data, "error_code", None) or getattr(observed, "error_code", None)
                )
                paused_for_user = (
                    first_error in {"CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "WAITING_FOR_LOGIN"}
                    or getattr(first_data, "status", None) == OperationStatus.WAITING_FOR_LOGIN
                    or _pause_reason(observed) in {"login_required", "captcha_required"}
                )
                retry_timeout = bool(getattr(observed, "timed_out", False)) and first_error == "timeout"
                if not paused_for_user and (retry_timeout or _transient_empty_feishu_observation(first_observation, normalized_url)):
                    if not retry_timeout:
                        await asyncio.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                    # ACKed reads are not replayed by transport. The timed-out
                    # operation is cancelled; one fresh observation may recover.
                    if review_dispatch_allowed():
                        observed = await observe_once(1)
            except asyncio.TimeoutError:
                # Preserve the dispatched read's audit identity even if the
                # caller's bounded wait ends before a response is returned.
                operation = None
                lookup = getattr(browser_bridge_store, "get_by_idempotency_key", None)
                if observation_request and callable(lookup):
                    try:
                        operation = lookup(observation_request.idempotency_key)
                    except Exception:
                        # A failed audit lookup must not replace the original
                        # timeout or abort reporting the other target records.
                        pass
                operation_id = getattr(operation, "operation_id", None)
                payload = getattr(operation, "result", None) or {}
                navigation = safe_review_navigation_diagnostics(payload.get("navigation_diagnostics")) if isinstance(payload, dict) else {}
                diagnostics = {"navigation_diagnostics": navigation} if navigation else {}
                return [
                    _result(item, "failed", started=group_started, reason="observation_timeout",
                            operation_id=operation_id, observation={"diagnostics": diagnostics} if diagnostics else None)
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
        diagnostic_result = getattr(data, "result", None)
        diagnostics = ({key: diagnostic_result[key] for key in
                        ("last_observation", "navigation_diagnostics", "auth_evidence", "auth_navigation", "page_url", "page", "semantic_nodes")
                        if key in diagnostic_result} if isinstance(diagnostic_result, dict) else {})
        if error_code in {"APPLICATION_RECORD_ENTRY_NOT_ENTERED", "APPLICATION_RECORD_HOME_REDIRECT"}:
            return [_result(item, "unresolved", started=group_started,
                            reason=error_code.casefold(), operation_id=operation_id,
                            observation={"diagnostics": diagnostics}) for item in application_ids]
        if error_code in {"FRAME_SCOPE_DENIED", "APPLICATION_PAGE_UNAVAILABLE"}:
            return [_result(item, "unresolved", started=group_started,
                            reason="unparsed_page" if error_code == "UNPARSED_APPLICATION_PAGE" else error_code.casefold(), operation_id=operation_id,
                            observation={"diagnostics": diagnostics}) for item in application_ids]

        if error_code == "AUTHENTICATION_RECOVERY_TIMEOUT":
            return [_result(item, "failed", started=group_started,
                            reason="authentication_recovery_timeout", operation_id=operation_id,
                            observation={"diagnostics": diagnostics} if diagnostics else None)
                    for item in application_ids]

        if error_code == "CAPTCHA_REQUIRED" or pause_reason == "captcha_required":
            state = "blocked" if request.skip_on_captcha else "failed"
            return [
                _result(
                    item,
                    state,
                    started=group_started,
                    reason="captcha_required",
                    operation_id=operation_id,
                    observation={"diagnostics": diagnostics} if diagnostics else None,
                )
                for item in application_ids
            ]
        if (
            error_code in {"LOGIN_REQUIRED", "WAITING_FOR_LOGIN"}
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
                    observation={"diagnostics": diagnostics} if diagnostics else None,
                )
                for item in application_ids
            ]

        observation = getattr(data, "observation", None)
        parser_unresolved = error_code in {"UNPARSED_APPLICATION_PAGE", "STATE_UNCLEAR"}
        if not isinstance(observation, dict) and isinstance(diagnostic_result, dict) and (
            parser_unresolved or (not error_code and operation_status == OperationStatus.STATE_UNCLEAR)
        ):
            # A stable readable page can fail extraction. Its bounded diagnostic
            # content authorizes a fresh image read, never a DOM write from the
            # unsuccessful operation. Access/navigation failures stay excluded.
            page = diagnostic_result.get("page") or {}
            raw_diagnostics = diagnostic_result.get("diagnostics") or {}
            if (isinstance(page, dict) and str(page.get("text") or "").strip()) or diagnostic_result.get("semantic_nodes") or raw_diagnostics.get("visibleMediaCount"):
                parser_unresolved = True
                observation = {"page": page, "semantic_nodes": diagnostic_result.get("semantic_nodes") or [],
                    "diagnostics": {**raw_diagnostics, **diagnostics}, "application_records": [], "entries": []}
        if not isinstance(observation, dict):
            return [
                _result(
                    item,
                    "unresolved" if error_code in {"UNPARSED_APPLICATION_PAGE", "STATE_UNCLEAR", ""} else "failed",
                    started=group_started,
                    reason="unparsed_page" if error_code == "UNPARSED_APPLICATION_PAGE" else
                           (pause_reason or error_code or "status_evidence_missing").casefold(),
                    operation_id=operation_id,
                    observation={"diagnostics": diagnostics} if diagnostics else None,
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
        async def with_model_fallback(results):
            # An incomplete parser must not decide which text the model can see.
            # Read the page images before making any remote status proposal.
            return await with_visual_fallback(results)

        async def with_visual_fallback(results):
            from packages.config import get_settings
            from packages.vision.service import VISION_MODELS
            from .application_status_model import MODEL_WAVE_DEADLINE, resolve_visual_statuses
            settings = get_settings()
            eligible = {item.application_id for item in results if item.state == "unresolved" and item.reason in {
                "unparsed_page", "application_records_missing", "record_present_status_unknown",
                "status_unmapped", "status_evidence_missing", "model_uncertain", "model_quote_not_found",
                "model_invalid_output", "status_semantics_unsupported", "model_evidence_scope_incomplete",
                "model_evidence_ref_invalid",
                "target_record_not_matched", "target_record_ambiguous", "target_card_not_unique",
                "model_identity_mismatch", "target_job_mismatch", "status_evidence_conflict",
                "visual_review_required", "confidence_below_threshold",
            }}
            if not eligible:
                return results

            def skip(disposition):
                return [item.model_copy(update={"vision_disposition": disposition, "diagnostics": {
                    **(item.diagnostics or {}), "vision_provider_request_count": 0,
                    "vision_analysis_count": 0, "vision_image_count": 0,
                }}) if item.application_id in eligible else item for item in results]

            if not request.include_vision:
                return skip("disabled_by_request")
            if not settings.vision_enabled:
                return skip("disabled_in_settings")
            if not (settings.write_enabled and settings.llm_enabled and settings.llm_api_key):
                return skip("not_configured")
            if getattr(settings, "vision_model", "deepseek-flash") not in VISION_MODELS:
                return skip("vision_model_unsupported")
            diagnostics = observation.get("diagnostics") or {}
            if not str((observation.get("page") or {}).get("text") or "").strip() and not (
                records or observation.get("semantic_nodes") or diagnostics.get("visibleMediaCount")
            ):
                return skip("skipped_blank_page")
            if diagnostics.get("scopeDeniedFrameCount") or diagnostics.get("unavailableFrameCount"):
                return skip("skipped_frame_restricted")
            deadline = MODEL_WAVE_DEADLINE.get()
            budget = min(70.0, deadline - perf_counter() - 3 if deadline else 70.0)
            if budget < 12:
                return [item.model_copy(update={"state": "failed", "reason": "review_wave_timeout",
                    "vision_disposition": "budget_deferred"}) if item.application_id in eligible else item for item in results]
            from .application_review_tasks import REVIEW_CONTEXT, review_dispatch_allowed
            owner = REVIEW_CONTEXT.get()
            if not review_dispatch_allowed():
                return skip("skipped_task_stopped")
            identifiers = [identifier for identifier in application_ids if identifier in eligible]
            visual_request = ObserveApplicationStatusPageInput(
                application_id=identifiers[0], application_ids=identifiers,
                application_url=raw_urls[normalized_url], timeout_ms=int(budget * 1000),
                include_vision=True, vision_fallback_reason="no_structured_evidence_visible_status_likely",
                reuse_observation_operation_id=operation_id,
                retain_on_pause=False, task_id=owner[1] if owner else None,
                idempotency_key=f"batch-vision-{operation_id}",
            )
            visual_id = None

            def visual_audit():
                nonlocal visual_id
                events = []
                if visual_id is None and callable(getattr(browser_bridge_store, "get_by_idempotency_key", None)):
                    operation = browser_bridge_store.get_by_idempotency_key(visual_request.idempotency_key)
                    visual_id = getattr(operation, "operation_id", None)
                if visual_id and callable(getattr(browser_bridge_store, "get_events", None)):
                    events = browser_bridge_store.get_events(visual_id)
                requests = [event for event in events if event.event_type == "vision_request"
                            and event.payload.get("provider_request_attempted") is True]
                errors = [event.payload for event in events if event.event_type == "vision_failure"]
                analyses = [event.payload for event in events if event.event_type == "vision_analysis"]
                return {
                    "vision_operation_id": visual_id,
                    "vision_provider_request_count": len(requests),
                    "vision_analysis_count": sum(event.event_type == "vision_analysis" for event in events),
                    "vision_image_count": sum(int(event.payload.get("image_count") or 0) for event in requests),
                    "vision_failure_diagnostics": [item.get("diagnostics") for item in errors
                                                   if isinstance(item.get("diagnostics"), (dict, list))],
                    "vision_card_diagnostics": [detail for item in analyses for detail in item.get("diagnostics", [])
                                                if isinstance(detail, dict) and str(detail.get("code", "")).startswith("card_")],
                }

            try:
                # One shared observation for the remaining jobs on this page.
                async with semaphore, origin_locks[concurrency_key]:
                    if not review_dispatch_allowed():
                        return skip("skipped_task_stopped")
                    visual = await asyncio.wait_for(observe_application_status_page_workflow(
                        visual_request, browser_bridge_store, repository), timeout=budget + 1)
                visual_data = getattr(visual, "data", None)
                visual_id = getattr(visual_data, "operation_id", None)
                captured = getattr(visual_data, "observation", None) or {}
                if isinstance(captured.get("vision"), dict):
                    outcomes = await resolve_visual_statuses(browser_bridge_store, visual_id,
                        [app for identifier, app in members if identifier in eligible], eligible)
                    return [item.model_copy(update={**outcomes.get(item.application_id, {}), "operation_id": visual_id,
                        "diagnostics": {**(item.diagnostics or {}),
                            **(outcomes.get(item.application_id, {}).get("diagnostics") or {}), **visual_audit()},
                        "vision_disposition": "analyzed", "elapsed_ms": _elapsed_ms(group_started)})
                        if item.application_id in eligible else item for item in results]
                error = captured.get("vision_error") or _pause_reason(visual) or _value(getattr(visual_data, "error_code", None)) or "visual_evidence_missing"
                return [item.model_copy(update={"vision_disposition": error, "diagnostics": {
                    **(item.diagnostics or {}), **visual_audit(), "vision_error": error}})
                    if item.application_id in eligible else item for item in results]
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                code = "vision_timeout"
            except Exception:
                code = "vision_failed"
            return [item.model_copy(update={"vision_disposition": code, "diagnostics": {
                **(item.diagnostics or {}), **visual_audit(), "vision_error": code,
            }}) if item.application_id in eligible else item for item in results]

        def missing_reason(application):
            if not records:
                return "application_records_missing"
            matches = matching_records(application, records)
            if not matches:
                return "target_record_not_matched"
            if len(matches) > 1:
                return "target_record_ambiguous"
            owners = [app for app in page_applications if any(card is matches[0] for card in matching_records(app, records))]
            if len(owners) != 1 or str(owners[0].id) != str(application.id):
                return "target_record_ambiguous"
            if status_evidence_conflict(matches[0]):
                return "status_evidence_conflict"
            if timeline_without_current(matches[0]) or not current_status_labels(matches[0]):
                return "record_present_status_unknown"
            unknown_label = any(explicit_label_status(label) is None for label in current_status_labels(matches[0]))
            return "status_unmapped" if has_unmapped_status(matches[0]) or unknown_label else "status_evidence_missing"

        def rule_needs_visual(application, card):
            from packages.config import get_settings
            settings = get_settings()
            if not (request.include_vision and settings.vision_enabled and settings.llm_enabled
                    and settings.write_enabled and settings.llm_api_key):
                return False
            coverage = (observation.get("diagnostics") or {}).get("textCoverage") or {}
            if coverage.get("truncated") or (card and (card.get("signals") or {}).get("context_truncated")):
                return True
            # Detect omitted volunteer cards independently of parsed record count.
            page_text = str((observation.get("page") or {}).get("text") or "")
            volunteers = set(re.findall(r"志愿\s*([一二三四五六七八九十\d]+)\s*[:：]", page_text))
            if len(volunteers) > len(records):
                return True
            # This exact, uniquely owned resume-routing label establishes only
            # continued processing. It neither proposes a stage nor needs the
            # parser's stage confidence (normally zero for noncanonical labels).
            from packages.domain.application_status_semantics import is_resume_routing_status
            if (card and current_status_labels(card)
                    and all(is_resume_routing_status(label) for label in current_status_labels(card))
                    and _matching_record_only(application, records, page_applications)):
                return False
            confidence = card.get("confidence") if card else None
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < _AUTO_CONFIDENCE:
                return True
            status = _normalise_status(card.get("status")) if card else None
            if card and any(explicit_label_status(label) is None for label in current_status_labels(card)):
                return True
            if status and any(explicit_label_status(label) != status for label in current_status_labels(card)):
                # A parser enum is not evidence when the literal current label
                # is unknown or contradicts it (e.g. applied + '流程中').
                return True
            saved = _value(getattr(application, "stage", ""))
            if status and status != saved and not (status == "applied" and saved != "interested"):
                return True
            # Legacy entries without a scoped target card need image evidence.
            return card is None and bool(entries)
        if parser_unresolved:
            return await with_visual_fallback([_result(item, "unresolved", started=group_started,
                reason="unparsed_page", operation_id=operation_id,
                observation=_compact_observation(observation)) for item in application_ids])
        if not isinstance(entries, list) or not entries:
            compact = _compact_observation(observation)
            results: list[ApplicationStatusResult] = []
            applications_by_id = {item[0]: item[1] for item in members}
            for item in application_ids:
                matches = matching_records(applications_by_id[item], records)
                if rule_needs_visual(applications_by_id[item], matches[0] if len(matches) == 1 else None):
                    results.append(_result(item, "unresolved", started=group_started,
                        reason="visual_review_required", operation_id=operation_id))
                elif _matching_record_only(applications_by_id[item], records, page_applications):
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
                        reason=missing_reason(applications_by_id[item]),
                        operation_id=operation_id,
                        observation=compact,
                    ))
            return await with_model_fallback(results)

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
            matches = matching_records(applications_by_id[application_id], records)
            if rule_needs_visual(applications_by_id[application_id], matches[0] if len(matches) == 1 else None):
                results.append(_result(application_id, "unresolved", started=group_started,
                    reason="visual_review_required", operation_id=operation_id))
                continue
            if _matching_record_only(applications_by_id[application_id], records, page_applications):
                results.append(_result(
                    application_id,
                    "unchanged",
                    started=group_started,
                    reason="no_newer_status_observed",
                    observed_status="applied",
                    operation_id=operation_id,
                ))
                continue
            if records and (len(matches) != 1 or has_unmapped_status(matches[0])
                            or missing_reason(applications_by_id[application_id]) in {
                                "target_record_ambiguous", "status_evidence_conflict", "record_present_status_unknown"}):
                results.append(_result(
                    application_id, "unresolved", started=group_started,
                    reason=missing_reason(applications_by_id[application_id]),
                    operation_id=operation_id, observation=_compact_observation(observation),
                ))
                continue
            application_terminal = terminal_result
            if len(matches) == 1 and _normalise_status(matches[0].get("status")) and matches[0].get("label"):
                card = matches[0]
                # The rule has bound this card, but the verifier still checks
                # its raw title independently; website IDs are not local IDs.
                application_terminal = {**terminal_result, "status": "", "entries": [{
                    "title": card.get("raw_title") or card.get("title"),
                    "external_application_id": str(card["application_id"]) if card.get("application_id") else None,
                    "external_job_id": str(card["job_id"]) if card.get("job_id") else None,
                    "status": card["status"], "label": card["label"],
                    "context": card.get("context") or card.get("evidence") or "",
                    "evidence": card.get("evidence") or card.get("context") or "",
                    "confidence": card.get("confidence"),
                    "stage_labels": card.get("stage_labels") or [],
                    "current_step_label": card.get("current_step_label") or "",
                    "signals": card.get("signals") or {},
                }]}
            from .application_review_tasks import review_write_guard
            with review_write_guard() as owns_claim:
                if not owns_claim:
                    results.append(_result(application_id, "failed", started=group_started,
                                           reason="review_owner_changed", operation_id=operation_id))
                    continue
                update = browser_status_update(
                    BrowserStatusUpdateInput(
                        application_id=application_id,
                        page_url=raw_urls[normalized_url],
                        terminal_result=application_terminal,
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
        return await with_model_fallback(results)

    grouped_results = await asyncio.gather(
        *(process_group(url, members) for url, members in groups.items()),
        return_exceptions=False,
    )
    results = immediate + [item for group in grouped_results for item in group]
    results = [review_result_presentation(item.model_copy(update={
        "company_name": applications[item.application_id].company_name,
        "job_title": applications[item.application_id].job_title,
        "saved_stage": _value(applications[item.application_id].stage),
    }) if item.application_id in applications else item) for item in results]
    checked_at = datetime.now(timezone.utc)
    results = [item.model_copy(update={"checked_at": checked_at}) for item in results]
    from packages.config import get_settings
    from packages.storage.application_reviews import save_latest_reviews
    from .application_review_tasks import REVIEW_CONTEXT
    storage = _storage(repository)
    if storage is not None and get_settings().write_enabled and REVIEW_CONTEXT.get() is None:
        with storage.write_transaction() as session:
            save_latest_reviews(session, [item.model_dump(mode="json") for item in results], checked_at=checked_at)
    buckets = {
        state: [item for item in results if item.state == state]
        for state in ("updated", "unchanged", "excluded", "blocked", "unresolved", "failed")
    }
    reconciled = len(buckets["updated"]) + len(buckets["unchanged"])
    settled = reconciled + len(buckets["excluded"])
    write_count = sum(item.wrote for item in buckets["updated"])
    all_reconciled = len(results) == len(selected_application_ids) and settled == len(results)
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
        "total": len(selected_application_ids),
        "pages_total": len(groups),
        "updated": len(buckets["updated"]),
        "unchanged": len(buckets["unchanged"]),
        "excluded": len(buckets["excluded"]),
        "excluded_mail_only": sum(item.reason == "mail_only" for item in buckets["excluded"]),
        "blocked": len(buckets["blocked"]),
        "unresolved": len(buckets["unresolved"]),
        "failed": len(buckets["failed"]),
        "write_count": write_count,
        "completion_rate": round(settled / len(selected_application_ids), 4),
        "selection": "all_non_terminal" if request.all_non_terminal else "explicit",
        "reason_breakdown": review_reason_breakdown(results),
        **review_presentation_summary(results),
        "scope_complete": len(results) == len(selected_application_ids),
        "verification_success_count": reconciled,
        "success_means": (
            "every eligible official-page application was verified as updated or unchanged; "
            "terminal and mail-only applications were excluded before browser access"
        ),
    }
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status",
        status=status,
        success=status is ToolStatus.SUCCESS,
        total=len(selected_application_ids),
        pages_total=len(groups),
        updated=buckets["updated"],
        unchanged=buckets["unchanged"],
        excluded=buckets["excluded"],
        blocked=buckets["blocked"],
        unresolved=buckets["unresolved"],
        failed=buckets["failed"],
        succeeded=buckets["updated"] + buckets["unchanged"],
        skipped=buckets["excluded"] + buckets["blocked"] + buckets["unresolved"],
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
