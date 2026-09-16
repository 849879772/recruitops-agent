"""Typed MCP boundaries for explicit recruitment-mail processing.

The processing service owns model triage, full analysis, application matching,
and guarded writes.  This module only validates the request and translates the
service result into the common typed-tool response contract.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from time import perf_counter
from typing import Any, Literal

from pydantic import Field, field_validator

from packages.config import Settings, get_settings
from packages.recruitment_mail import RecruitmentMailStore
from packages.repositories.base import RecruitmentRepository
from packages.security import redact_sensitive

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus


class RecruitmentMailProcessInput(ToolInput):
    """Bounded input for one explicit processing pass."""

    # The processing service owns a 90-second total budget and caps one model
    # request at 25 seconds; the generic five-second ToolInput default is not
    # a processing deadline.
    timeout_ms: int = Field(default=90_000, ge=1, le=90_000)
    limit: int = Field(default=20, ge=1, le=50)
    record_ids: list[str] | None = Field(default=None, max_length=50)

    @field_validator("record_ids")
    @classmethod
    def normalize_record_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("record_ids must contain non-empty strings")
            normalized.append(item)
        return normalized


class RecruitmentMailProcessingStatusInput(ToolInput):
    """Bounded read-only status request; it never synchronizes the mailbox."""

    limit: int = Field(default=50, ge=1, le=50)
    include_history: bool = False


class RecruitmentMailProcessResponse(ToolResponse[dict[str, Any]]):
    freshness: dict[str, Any] | None = None
    read_only: Literal[False] = False


class RecruitmentMailProcessingStatusResponse(ToolResponse[dict[str, Any]]):
    pass


ProcessingService = Callable[..., dict[str, Any]]


def _load_processing_service() -> tuple[ProcessingService, ProcessingService]:
    """Load the parent-owned service only when a tool is actually invoked."""

    module = import_module("packages.recruitment_mail.processing")
    return module.process_pending_mail, module.processing_status


def _evidence(source_ref: str) -> list[EvidenceSource]:
    return [EvidenceSource(source="agent_recruitment_mail", source_ref=source_ref)]


def _safe_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("mail processing service must return a dictionary")
    redacted = redact_sensitive(value)
    if not isinstance(redacted, dict):
        raise TypeError("mail processing service returned an invalid dictionary")
    return redacted


def _error_code_from(value: object, default: ToolErrorCode) -> ToolErrorCode:
    try:
        return ToolErrorCode(str(getattr(value, "value", value)))
    except (TypeError, ValueError):
        return default


def _result_error_code(payload: dict[str, Any]) -> ToolErrorCode:
    reason = str(payload.get("reason") or "").strip().casefold()
    if reason == "write_disabled":
        return ToolErrorCode.READ_ONLY_VIOLATION
    return _error_code_from(
        payload.get("error_code") or payload.get("code"),
        ToolErrorCode.SOURCE_UNAVAILABLE,
    )


def _exception_error_code(exc: BaseException) -> ToolErrorCode:
    if isinstance(exc, PermissionError):
        return ToolErrorCode.READ_ONLY_VIOLATION
    if isinstance(exc, TimeoutError):
        return ToolErrorCode.TIMEOUT
    if isinstance(exc, KeyError):
        return ToolErrorCode.NOT_FOUND
    if isinstance(exc, (TypeError, ValueError)):
        return ToolErrorCode.INVALID_INPUT
    return ToolErrorCode.SOURCE_UNAVAILABLE


def _error_message(code: ToolErrorCode, *, processing: bool) -> str:
    if code is ToolErrorCode.READ_ONLY_VIOLATION:
        return "Explicit mail processing requires Settings.write_enabled=true."
    if code is ToolErrorCode.TIMEOUT:
        return "The bounded recruitment-mail operation timed out."
    if code is ToolErrorCode.NOT_FOUND:
        return "The requested recruitment-mail record was not found."
    if code is ToolErrorCode.INVALID_INPUT:
        return "The recruitment-mail operation received invalid input."
    if processing:
        return "The recruitment-mail processing service was unavailable."
    return "The recruitment-mail processing status was unavailable."


def _service_failed(payload: dict[str, Any]) -> bool:
    if payload.get("success") is False:
        return True
    status = str(payload.get("status") or "").strip().casefold()
    return status in {"failed", "failure", "error", "blocked"}


def _response_failure(
    request: ToolInput,
    *,
    response_type: type[ToolResponse[Any]],
    code: ToolErrorCode,
    processing: bool,
    data: dict[str, Any] | None = None,
    timed_out: bool = False,
) -> ToolResponse[Any]:
    return response_type(
        tool_name=(
            "recruitment_mail_process"
            if processing
            else "recruitment_mail_processing_status"
        ),
        status=ToolStatus.FAILURE,
        success=False,
        data=data,
        evidence=_evidence("processing" if processing else "processing_status"),
        error_code=code,
        error_message=_error_message(code, processing=processing),
        timeout_ms=request.timeout_ms,
        timed_out=timed_out,
        elapsed_ms=0,
        read_only=not processing,
    )


def _run_service(
    request: ToolInput,
    *,
    response_type: type[ToolResponse[Any]],
    processing: bool,
    call: Callable[[], dict[str, Any]],
) -> ToolResponse[Any]:
    started = perf_counter()
    try:
        payload = _safe_payload(call())
    except Exception as exc:
        response = _response_failure(
            request,
            response_type=response_type,
            code=_exception_error_code(exc),
            processing=processing,
            timed_out=isinstance(exc, TimeoutError),
        )
        return response.model_copy(
            update={"elapsed_ms": max(0, int((perf_counter() - started) * 1_000))}
        )

    elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
    failed = _service_failed(payload)
    code = _result_error_code(payload)
    response = response_type(
        tool_name=(
            "recruitment_mail_process"
            if processing
            else "recruitment_mail_processing_status"
        ),
        status=ToolStatus.FAILURE if failed else ToolStatus.SUCCESS,
        success=not failed,
        data=payload,
        evidence=_evidence("processing" if processing else "processing_status"),
        error_code=code if failed else None,
        error_message=_error_message(code, processing=processing) if failed else None,
        timeout_ms=request.timeout_ms,
        timed_out=False,
        elapsed_ms=elapsed_ms,
        read_only=not processing,
    )
    return response


def recruitment_mail_process(
    request: RecruitmentMailProcessInput,
    store: RecruitmentMailStore,
    repository: RecruitmentRepository,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
) -> RecruitmentMailProcessResponse:
    """Run one explicit bounded processing pass through the parent service."""

    started = perf_counter()
    try:
        effective_settings = settings if settings is not None else get_settings()
        if not bool(getattr(effective_settings, "write_enabled", False)):
            response = _response_failure(
                request,
                response_type=RecruitmentMailProcessResponse,
                code=ToolErrorCode.READ_ONLY_VIOLATION,
                processing=True,
            )
            return response.model_copy(
                update={"elapsed_ms": max(0, int((perf_counter() - started) * 1_000))}
            )
        process_pending_mail, _processing_status = _load_processing_service()
    except Exception as exc:
        response = _response_failure(
            request,
            response_type=RecruitmentMailProcessResponse,
            code=_exception_error_code(exc),
            processing=True,
            timed_out=isinstance(exc, TimeoutError),
        )
        return response.model_copy(
            update={"elapsed_ms": max(0, int((perf_counter() - started) * 1_000))}
        )

    return _run_service(
        request,
        response_type=RecruitmentMailProcessResponse,
        processing=True,
        call=lambda: process_pending_mail(
            store,
            repository,
            effective_settings,
            limit=request.limit,
            record_ids=request.record_ids,
            client=client,
        ),
    )


def recruitment_mail_processing_status(
    request: RecruitmentMailProcessingStatusInput,
    store: RecruitmentMailStore,
) -> RecruitmentMailProcessingStatusResponse:
    """Read persisted processing state without mailbox synchronization or writes."""

    return _run_service(
        request,
        response_type=RecruitmentMailProcessingStatusResponse,
        processing=False,
        call=lambda: _load_processing_service()[1](store, limit=request.limit,
            **({"include_history": True} if request.include_history else {})),
    )


# Verb-first aliases are convenient for callers that mirror the service name.
process_recruitment_mail = recruitment_mail_process
get_recruitment_mail_processing_status = recruitment_mail_processing_status


__all__ = [
    "RecruitmentMailProcessInput",
    "RecruitmentMailProcessResponse",
    "RecruitmentMailProcessingStatusInput",
    "RecruitmentMailProcessingStatusResponse",
    "get_recruitment_mail_processing_status",
    "process_recruitment_mail",
    "recruitment_mail_process",
    "recruitment_mail_processing_status",
]
