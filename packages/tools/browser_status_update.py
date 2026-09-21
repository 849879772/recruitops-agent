"""Validate an Edge application-status result and update the Agent snapshot safely."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.approval import AgentApplicationWriteAdapter
from packages.domain.models import ApplicationStage
from packages.domain.urls import normalize_http_page_url
from packages.storage import ApplicationSnapshot, Storage, WriteAudit


_AUTO_CONFIDENCE = 0.90
_OPERATION_TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "STATE_UNCLEAR", "FAILED", "CANCELLED"}
)
_STAGE_ORDER = {
    ApplicationStage.INTERESTED: 0,
    ApplicationStage.APPLIED: 1,
    ApplicationStage.ASSESSMENT: 2,
    ApplicationStage.WRITTEN: 3,
    ApplicationStage.INTERVIEW1: 4,
    ApplicationStage.INTERVIEW2: 5,
    ApplicationStage.INTERVIEW3: 6,
    ApplicationStage.HR: 7,
    ApplicationStage.OFFER: 8,
    ApplicationStage.REJECTED: 9,
    ApplicationStage.WITHDRAWN: 9,
}
_TERMINAL_STAGES = {ApplicationStage.REJECTED, ApplicationStage.WITHDRAWN}
_STATUS_ALIASES = {
    "interested": "interested",
    "applied": "applied",
    "assessment": "assessment",
    "written": "written",
    "interview": "interview",
    "hr": "hr",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "已投递": "applied",
    "投递成功": "applied",
    "筛选阶段": "applied",
    "筛选中": "applied",
    "测试中": "applied",
    "测试阶段": "applied",
    "进行中": "applied",
    "测评": "applied",
    "测评中": "applied",
    "在线测评": "applied",
    "线上测评": "applied",
    "线上测评_进行中": "applied",
    "笔试": "written",
    "笔试中": "written",
    "面试": "interview",
    "录用": "offer",
    "已拒绝": "rejected",
    "已撤回": "withdrawn",
}
_STATE_UNCLEAR_CODES = {
    "application_not_found",
    "application_record_not_unique",
    "record_url_missing_or_invalid",
    "observation_url_mismatch",
    "target_job_invalid",
    "target_job_mismatch",
    "operation_state_unclear",
    "status_evidence_missing",
    "status_evidence_unknown",
    "status_evidence_conflict",
    "status_entry_ambiguous",
    "status_entry_not_matched",
    "status_label_missing",
    "status_captured_at_missing",
    "confidence_below_threshold",
    "current_stage_invalid",
}
_WRITE_LOCK = RLock()


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class _BrowserResultModel(_Model):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class UpdateStatus(StrEnum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    APPROVAL_REQUIRED = "approval_required"
    STATE_UNCLEAR = "STATE_UNCLEAR"
    FAILED = "failed"


class MatchMethod(StrEnum):
    APPLICATION_ID = "application_id_match"
    UNIQUE_PAGE = "unique_page_match"
    UNIQUE_TITLE_CONTEXT = "unique_title_context"


class BrowserStatusEntry(_BrowserResultModel):
    application_id: str | None = Field(
        default=None,
        max_length=255,
        validation_alias=AliasChoices("application_id", "applicationId"),
    )
    status: str = Field(default="", max_length=128)
    label: str = Field(default="", max_length=200)
    context: str = Field(default="", max_length=1_000)
    evidence: str = Field(default="", max_length=2_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("application_id", mode="before")
    @classmethod
    def coerce_application_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("application_id must be a string or integer")
        value = str(value).strip()
        return value or None


class BrowserOperationTerminalResult(_BrowserResultModel):
    """The bounded status payload carried by a terminal BrowserOperation result."""

    operation_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=255)
    operation_status: str | None = Field(default=None, max_length=32)
    status: str = Field(default="", max_length=128)
    label: str = Field(default="", max_length=200)
    context: str = Field(default="", max_length=1_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    entries: list[BrowserStatusEntry] = Field(default_factory=list, max_length=100)
    captured_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("captured_at", "capturedAt"),
    )

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("capturedAt must include a timezone")
        return value


class BrowserStatusUpdateInput(_Model):
    application_id: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("application_id", "id"),
    )
    page_url: str = Field(
        min_length=1,
        max_length=2_048,
        validation_alias=AliasChoices("page_url", "url"),
    )
    terminal_result: BrowserOperationTerminalResult = Field(
        validation_alias=AliasChoices(
            "terminal_result",
            "browser_operation",
            "result",
            "data",
        )
    )
    operation_id: str | None = Field(default=None, max_length=128)

    @field_validator("application_id", mode="before")
    @classmethod
    def coerce_application_id(cls, value: object) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError("application_id is required")
        return str(value).strip()

    @field_validator("page_url")
    @classmethod
    def validate_page_url(cls, value: str) -> str:
        if normalize_http_page_url(value) is None:
            raise ValueError("page_url must be an HTTP(S) URL without credentials")
        return value

    @field_validator("terminal_result", mode="before")
    @classmethod
    def parse_terminal_result(cls, value: object) -> dict[str, Any]:
        return _unwrap_terminal_result(value)

    @model_validator(mode="after")
    def derive_operation_id(self) -> BrowserStatusUpdateInput:
        if self.operation_id is None and self.terminal_result.operation_id:
            object.__setattr__(self, "operation_id", self.terminal_result.operation_id)
        return self


class BrowserStatusUpdateData(_Model):
    application_id: str
    page_url: str
    record_url: str | None = None
    current_stage: ApplicationStage | None = None
    target_stage: ApplicationStage | None = None
    observed_status: str | None = None
    observed_label: str | None = None
    captured_at: datetime | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_method: MatchMethod | None = None
    state: UpdateStatus
    reason_code: str
    reason: str
    wrote: bool = False
    requires_approval: bool = False
    idempotent_replay: bool = False
    audit_id: str
    idempotency_key: str


class BrowserStatusUpdateResponse(_Model):
    tool_name: str = "browser_status_update"
    status: UpdateStatus
    success: bool
    data: BrowserStatusUpdateData | None = None
    error_code: str | None = None
    error_message: str | None = None
    audit_id: str
    audit_persisted: bool

    @model_validator(mode="after")
    def validate_result_state(self) -> BrowserStatusUpdateResponse:
        expected_success = self.status in {UpdateStatus.UPDATED, UpdateStatus.UNCHANGED}
        if self.success != expected_success:
            raise ValueError("success must agree with status")
        if expected_success and self.error_code is not None:
            raise ValueError("successful status updates cannot contain an error code")
        if not expected_success and not self.error_code:
            raise ValueError("non-success status updates require an error code")
        return self


@dataclass(frozen=True)
class _Match:
    entry: BrowserStatusEntry
    confidence: float
    method: MatchMethod


def _as_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=False)
    values: dict[str, Any] = {}
    for key in (
        "operation_id",
        "idempotency_key",
        "status",
        "result",
        "terminal_result",
        "data",
    ):
        if hasattr(value, key):
            values[key] = getattr(value, key)
    return values or None


def _is_operation_status(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() in _OPERATION_TERMINAL_STATUSES


def _unwrap_terminal_result(value: object) -> dict[str, Any]:
    current = _as_mapping(value)
    if current is None:
        raise ValueError("terminal_result must be an object")

    inherited: dict[str, Any] = {}
    for _ in range(5):
        for key in ("operation_id", "idempotency_key"):
            if current.get(key) is not None and inherited.get(key) is None:
                inherited[key] = current[key]
        if _is_operation_status(current.get("status")):
            inherited["operation_status"] = str(current["status"]).strip().upper()

        nested: dict[str, Any] | None = None
        for key in ("terminal_result", "result", "data"):
            candidate = current.get(key)
            candidate_mapping = _as_mapping(candidate)
            if candidate_mapping is not None:
                nested = candidate_mapping
                break
        if nested is None:
            break
        current = nested

    result = dict(current)
    result.update({key: value for key, value in inherited.items() if value is not None})
    if _is_operation_status(result.get("status")):
        result["operation_status"] = str(result["status"]).strip().upper()
        result["status"] = ""
    return result


def _normalise_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s-]+", "_", value.strip().casefold())
    return _STATUS_ALIASES.get(key)


def _text_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _title_matches(title: str, text: str) -> bool:
    compact_title = re.sub(r"\s+", "", title.casefold())
    compact_text = re.sub(r"\s+", "", text.casefold())
    if compact_title and compact_title in compact_text:
        return True
    title_key = _text_key(title)
    context_key = _text_key(text)
    return len(title_key) >= 3 and title_key in context_key


def _context_text(entry: BrowserStatusEntry) -> str:
    return " ".join(item for item in (entry.context, entry.evidence) if item).strip()


def _normalise_entries(
    result: BrowserOperationTerminalResult,
) -> tuple[list[BrowserStatusEntry], str | None]:
    top_status = _normalise_status(result.status)
    raw_top_status = result.status.strip().casefold()
    if (
        result.status.strip()
        and raw_top_status != "multiple"
        and top_status is None
        and not _is_operation_status(result.status)
    ):
        return [], "status_evidence_unknown"

    entries = list(result.entries)
    if not entries and top_status:
        entries = [
            BrowserStatusEntry(
                status=top_status,
                label=result.label,
                context=result.context,
                confidence=result.confidence,
            )
        ]
    elif len(entries) == 1:
        entry = entries[0]
        updates: dict[str, Any] = {}
        if not entry.status and top_status:
            updates["status"] = top_status
        if not entry.label and result.label:
            updates["label"] = result.label
        if not entry.context and result.context:
            updates["context"] = result.context
        if entry.confidence is None and result.confidence is not None:
            updates["confidence"] = result.confidence
        if updates:
            entries[0] = entry.model_copy(update=updates)

    if not entries:
        return [], "status_evidence_missing"
    if top_status and raw_top_status != "multiple" and len(entries) == 1:
        entry_status = _normalise_status(entries[0].status)
        if entry_status and entry_status != top_status:
            return [], "status_evidence_conflict"
    return entries, None


def _application_snapshot_diff(application: ApplicationSnapshot) -> dict[str, Any]:
    return {
        "application_id": application.id,
        "company_name": application.company_name,
        "job_title": application.job_title,
        "record_url": normalize_http_page_url(application.record_url or ""),
        "stage": application.stage,
        "source_stage": application.source_stage,
        "source_status": application.source_status,
    }


def _context_is_consistent(
    application: ApplicationSnapshot,
    page_applications: list[ApplicationSnapshot],
    entry: BrowserStatusEntry,
) -> bool:
    context = _context_text(entry)
    if not context:
        return True
    matching_titles = [
        item for item in page_applications if _title_matches(item.job_title, context)
    ]
    if not matching_titles:
        return True
    return len(matching_titles) == 1 and matching_titles[0].id == application.id


def _match_entry(
    application: ApplicationSnapshot,
    page_applications: list[ApplicationSnapshot],
    entries: list[BrowserStatusEntry],
) -> tuple[_Match | None, str]:
    explicit_target = [
        entry
        for entry in entries
        if entry.application_id and entry.application_id == application.id
    ]
    if len(explicit_target) > 1:
        return None, "status_entry_ambiguous"
    if len(explicit_target) == 1:
        entry = explicit_target[0]
        if not _context_is_consistent(application, page_applications, entry):
            return None, "target_job_mismatch"
        return _Match(entry, 0.99, MatchMethod.APPLICATION_ID), ""

    explicit_other = [entry for entry in entries if entry.application_id]
    if len(page_applications) == 1 and len(entries) == 1 and not explicit_other:
        return _Match(entries[0], 0.95, MatchMethod.UNIQUE_PAGE), ""

    contextual = [
        entry
        for entry in entries
        if not entry.application_id and _title_matches(application.job_title, _context_text(entry))
    ]
    if len(contextual) > 1:
        return None, "status_entry_ambiguous"
    if len(contextual) == 1:
        matching_titles = [
            item
            for item in page_applications
            if _title_matches(item.job_title, _context_text(contextual[0]))
        ]
        if len(matching_titles) == 1 and matching_titles[0].id == application.id:
            return _Match(contextual[0], 0.90, MatchMethod.UNIQUE_TITLE_CONTEXT), ""
        return None, "target_job_mismatch"
    return None, "status_entry_not_matched"


def _effective_confidence(
    result: BrowserOperationTerminalResult,
    match: _Match,
) -> float:
    values = [match.confidence]
    if result.confidence is not None:
        values.append(result.confidence)
    if match.entry.confidence is not None:
        values.append(match.entry.confidence)
    return min(values)


def _target_stage(current: ApplicationStage, observed_status: str) -> ApplicationStage:
    if observed_status == "interview":
        if current in {
            ApplicationStage.INTERVIEW1,
            ApplicationStage.INTERVIEW2,
            ApplicationStage.INTERVIEW3,
            ApplicationStage.HR,
            ApplicationStage.OFFER,
        }:
            return current
        return ApplicationStage.INTERVIEW1
    mapping = {
        "interested": ApplicationStage.INTERESTED,
        "applied": ApplicationStage.APPLIED,
        "assessment": ApplicationStage.APPLIED,
        "written": ApplicationStage.WRITTEN,
        "hr": ApplicationStage.HR,
        "offer": ApplicationStage.OFFER,
        "rejected": ApplicationStage.REJECTED,
        "withdrawn": ApplicationStage.WITHDRAWN,
    }
    return mapping[observed_status]


def _identity(request: BrowserStatusUpdateInput) -> tuple[str, str, str, str]:
    operation_id = request.operation_id or request.terminal_result.operation_id
    canonical = {
        "application_id": request.application_id,
        "page_url": normalize_http_page_url(request.page_url),
        "operation_id": operation_id,
        "terminal_result": request.terminal_result.model_dump(mode="json", exclude_none=True),
    }
    fingerprint = sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    idempotency_key = f"browser-status-update:{fingerprint}"
    execution_id = f"browser-status-update:{fingerprint[:48]}"
    token_id = f"edge-status:{fingerprint[:32]}"
    task_id = f"edge-status:{fingerprint[32:64]}"
    return idempotency_key, execution_id, token_id, task_id


def _evidence(
    request: BrowserStatusUpdateInput,
    audit_id: str,
    application: ApplicationSnapshot | None,
    entry: BrowserStatusEntry | None,
    *,
    confidence: float,
    match_method: MatchMethod | None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "source": "edge.browser_operation",
            "source_ref": request.operation_id or audit_id,
            "field": "page_url",
            "value": normalize_http_page_url(request.page_url),
        },
        {
            "source": "agent.application_snapshot",
            "source_ref": request.application_id,
            "field": "application_id",
            "value": request.application_id,
        },
    ]
    if application is not None:
        evidence.append(
            {
                "source": "agent.application_snapshot",
                "source_ref": application.id,
                "field": "job_title",
                "value": application.job_title,
            }
        )
    if entry is not None:
        evidence.append(
            {
                "source": "edge.status_entry",
                "source_ref": entry.application_id or match_method.value if match_method else audit_id,
                "field": "status",
                "value": _normalise_status(entry.status) or entry.status,
                "text": entry.label,
                "confidence": confidence,
            }
        )
    return evidence


def _new_audit(
    *,
    request: BrowserStatusUpdateInput,
    audit_id: str,
    idempotency_key: str,
    token_id: str,
    task_id: str,
    started_at: datetime,
    evidence: list[dict[str, Any]],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    success: bool,
    error_code: str | None,
    rollback: dict[str, Any] | None = None,
) -> WriteAudit:
    evidence_digest = sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return WriteAudit(
        execution_id=audit_id,
        token_id=token_id,
        task_id=task_id,
        operation="application_stage_update",
        idempotency_key=idempotency_key,
        operator="edge-browser",
        evidence=evidence,
        evidence_digest=evidence_digest,
        before_diff=before,
        after_diff=after,
        rollback_payload=rollback,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        success=success,
        error_code=error_code,
    )


def _find_audit(storage: Storage, idempotency_key: str) -> WriteAudit | None:
    with storage.session() as session:
        return session.scalar(
            select(WriteAudit).where(WriteAudit.idempotency_key == idempotency_key)
        )


def _insert_audit(storage: Storage, audit: WriteAudit) -> tuple[WriteAudit, bool]:
    try:
        with storage.write_transaction() as session:
            existing = session.scalar(
                select(WriteAudit).where(WriteAudit.idempotency_key == audit.idempotency_key)
            )
            if existing is not None:
                return existing, False
            session.add(audit)
            session.flush()
            return audit, True
    except IntegrityError:
        existing = _find_audit(storage, audit.idempotency_key)
        if existing is not None:
            return existing, False
        raise


def _finish_audit(
    storage: Storage,
    *,
    idempotency_key: str,
    audit_id: str,
    evidence: list[dict[str, Any]],
    started_at: datetime,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    success: bool,
    error_code: str | None,
    rollback: dict[str, Any] | None = None,
) -> WriteAudit:
    with storage.write_transaction() as session:
        audit = session.scalar(
            select(WriteAudit)
            .where(WriteAudit.idempotency_key == idempotency_key)
            .with_for_update()
        )
        if audit is None:
            raise RuntimeError("write audit record was not created")
        if audit.execution_id != audit_id or audit.success:
            return audit
        audit.evidence = evidence
        audit.before_diff = before
        audit.after_diff = after
        audit.rollback_payload = rollback
        audit.completed_at = datetime.now(timezone.utc)
        audit.success = success
        audit.error_code = error_code
        session.flush()
        return audit


def _stage_or_none(value: object) -> ApplicationStage | None:
    try:
        return ApplicationStage(str(value))
    except (TypeError, ValueError):
        return None


def _data(
    request: BrowserStatusUpdateInput,
    *,
    audit_id: str,
    idempotency_key: str,
    status: UpdateStatus,
    reason_code: str,
    reason: str,
    application: ApplicationSnapshot | None = None,
    record_url: str | None = None,
    current_stage: ApplicationStage | None = None,
    target_stage: ApplicationStage | None = None,
    entry: BrowserStatusEntry | None = None,
    captured_at: datetime | None = None,
    confidence: float = 0.0,
    match_method: MatchMethod | None = None,
    wrote: bool = False,
    requires_approval: bool = False,
    idempotent_replay: bool = False,
) -> BrowserStatusUpdateData:
    return BrowserStatusUpdateData(
        application_id=request.application_id,
        page_url=normalize_http_page_url(request.page_url) or request.page_url,
        record_url=record_url,
        current_stage=current_stage,
        target_stage=target_stage,
        observed_status=(
            _normalise_status(entry.status) or entry.status if entry is not None else None
        ),
        observed_label=entry.label if entry is not None else None,
        captured_at=captured_at,
        confidence=confidence,
        match_method=match_method,
        state=status,
        reason_code=reason_code,
        reason=reason,
        wrote=wrote,
        requires_approval=requires_approval,
        idempotent_replay=idempotent_replay,
        audit_id=audit_id,
        idempotency_key=idempotency_key,
    )


def _response(
    *,
    status: UpdateStatus,
    data: BrowserStatusUpdateData | None,
    audit_id: str,
    audit_persisted: bool,
    error_code: str | None = None,
    error_message: str | None = None,
) -> BrowserStatusUpdateResponse:
    success = status in {UpdateStatus.UPDATED, UpdateStatus.UNCHANGED}
    return BrowserStatusUpdateResponse(
        status=status,
        success=success,
        data=data,
        error_code=None if success else error_code or status.value,
        error_message=None if success else error_message,
        audit_id=audit_id,
        audit_persisted=audit_persisted,
    )


def _response_from_audit(
    request: BrowserStatusUpdateInput,
    audit: WriteAudit,
    *,
    idempotency_key: str,
) -> BrowserStatusUpdateResponse:
    before = audit.before_diff or {}
    after = audit.after_diff or {}
    current_stage = _stage_or_none(before.get("stage"))
    target_stage = _stage_or_none(after.get("stage"))
    confidence = float(after.get("confidence") or 0.0)
    match_method = after.get("match_method")
    try:
        match_method_value = MatchMethod(str(match_method)) if match_method else None
    except ValueError:
        match_method_value = None
    entry = (
        BrowserStatusEntry(
            status=str(after.get("source_stage") or ""),
            label=str(after.get("source_status") or ""),
        )
        if after.get("source_stage") or after.get("source_status")
        else None
    )
    record_url = after.get("record_url") or before.get("record_url")
    if audit.success:
        data = _data(
            request,
            audit_id=audit.execution_id,
            idempotency_key=idempotency_key,
            status=UpdateStatus.UNCHANGED,
            reason_code="idempotent_replay",
            reason="The same Edge terminal result was already processed successfully.",
            record_url=record_url,
            current_stage=target_stage or current_stage,
            target_stage=target_stage,
            entry=entry,
            captured_at=request.terminal_result.captured_at,
            confidence=confidence,
            match_method=match_method_value,
            idempotent_replay=True,
        )
        return _response(
            status=UpdateStatus.UNCHANGED,
            data=data,
            audit_id=audit.execution_id,
            audit_persisted=True,
        )

    reason_code = str(after.get("reason_code") or audit.error_code or "write_failed")
    status = (
        UpdateStatus.APPROVAL_REQUIRED
        if audit.error_code == UpdateStatus.APPROVAL_REQUIRED.value
        else UpdateStatus.STATE_UNCLEAR
        if audit.error_code in _STATE_UNCLEAR_CODES
        else UpdateStatus.FAILED
    )
    data = _data(
        request,
        audit_id=audit.execution_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
        reason=str(after.get("reason") or "The previous status update attempt did not write successfully."),
        record_url=record_url,
        current_stage=current_stage,
        target_stage=target_stage,
        entry=entry,
        captured_at=request.terminal_result.captured_at,
        confidence=confidence,
        match_method=match_method_value,
        requires_approval=status is UpdateStatus.APPROVAL_REQUIRED,
    )
    return _response(
        status=status,
        data=data,
        audit_id=audit.execution_id,
        audit_persisted=True,
        error_code=audit.error_code or status.value,
        error_message=data.reason,
    )


def _persist_decision(
    storage: Storage,
    *,
    request: BrowserStatusUpdateInput,
    audit_id: str,
    idempotency_key: str,
    token_id: str,
    task_id: str,
    started_at: datetime,
    status: UpdateStatus,
    reason_code: str,
    reason: str,
    evidence: list[dict[str, Any]],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    application: ApplicationSnapshot | None,
    record_url: str | None,
    current_stage: ApplicationStage | None,
    target_stage: ApplicationStage | None,
    entry: BrowserStatusEntry | None,
    captured_at: datetime | None,
    confidence: float,
    match_method: MatchMethod | None,
    requires_approval: bool = False,
) -> BrowserStatusUpdateResponse:
    audit_error_code = (
        UpdateStatus.APPROVAL_REQUIRED.value if status is UpdateStatus.APPROVAL_REQUIRED else reason_code
    )
    if after is not None:
        after = {**after, "reason_code": reason_code, "reason": reason}
    audit = _new_audit(
        request=request,
        audit_id=audit_id,
        idempotency_key=idempotency_key,
        token_id=token_id,
        task_id=task_id,
        started_at=started_at,
        evidence=evidence,
        before=before,
        after=after,
        success=False,
        error_code=audit_error_code,
    )
    try:
        saved, created = _insert_audit(storage, audit)
    except Exception:
        data = _data(
            request,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            status=UpdateStatus.FAILED,
            reason_code="audit_persistence_failed",
            reason="The status decision could not be persisted.",
            application=application,
            record_url=record_url,
            current_stage=current_stage,
            target_stage=target_stage,
            entry=entry,
            captured_at=captured_at,
            confidence=confidence,
            match_method=match_method,
            requires_approval=False,
        )
        return _response(
            status=UpdateStatus.FAILED,
            data=data,
            audit_id=audit_id,
            audit_persisted=False,
            error_code="audit_persistence_failed",
            error_message=data.reason,
        )
    if not created:
        return _response_from_audit(request, saved, idempotency_key=idempotency_key)
    data = _data(
        request,
        audit_id=audit_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
        reason=reason,
        application=application,
        record_url=record_url,
        current_stage=current_stage,
        target_stage=target_stage,
        entry=entry,
        captured_at=captured_at,
        confidence=confidence,
        match_method=match_method,
        requires_approval=requires_approval,
    )
    return _response(
        status=status,
        data=data,
        audit_id=audit_id,
        audit_persisted=True,
        error_code=(
            UpdateStatus.APPROVAL_REQUIRED.value
            if status is UpdateStatus.APPROVAL_REQUIRED
            else reason_code
        ),
        error_message=reason,
    )


def browser_status_update(
    request: BrowserStatusUpdateInput,
    storage: Storage,
    *,
    adapter: AgentApplicationWriteAdapter | None = None,
) -> BrowserStatusUpdateResponse:
    """Validate one terminal Edge result and apply only a safe forward stage update."""

    if not isinstance(request, BrowserStatusUpdateInput):
        request = BrowserStatusUpdateInput.model_validate(request)
    idempotency_key, audit_id, token_id, task_id = _identity(request)
    started_at = datetime.now(timezone.utc)
    normalized_page_url = normalize_http_page_url(request.page_url) or request.page_url

    with _WRITE_LOCK:
        try:
            storage.initialize()
            existing = _find_audit(storage, idempotency_key)
        except Exception:
            data = _data(
                request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                status=UpdateStatus.FAILED,
                reason_code="agent_storage_unavailable",
                reason="The Agent application store could not be read.",
            )
            return _response(
                status=UpdateStatus.FAILED,
                data=data,
                audit_id=audit_id,
                audit_persisted=False,
                error_code="agent_storage_unavailable",
                error_message=data.reason,
            )
        if existing is not None:
            return _response_from_audit(request, existing, idempotency_key=idempotency_key)

        operation_status = request.terminal_result.operation_status
        if operation_status and operation_status != "SUCCEEDED":
            status = (
                UpdateStatus.STATE_UNCLEAR
                if operation_status == "STATE_UNCLEAR"
                else UpdateStatus.FAILED
            )
            reason_code = (
                "operation_state_unclear"
                if status is UpdateStatus.STATE_UNCLEAR
                else "browser_operation_failed"
            )
            reason = "The Edge operation did not return a successful terminal result."
            evidence = _evidence(
                request,
                audit_id,
                None,
                None,
                confidence=0.0,
                match_method=None,
            )
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=status,
                reason_code=reason_code,
                reason=reason,
                evidence=evidence,
                before=None,
                after=None,
                application=None,
                record_url=None,
                current_stage=None,
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )

        application: ApplicationSnapshot | None = None
        page_applications: list[ApplicationSnapshot] = []
        try:
            with storage.session() as session:
                applications = session.scalars(
                    select(ApplicationSnapshot).where(
                        ApplicationSnapshot.id == request.application_id
                    )
                ).all()
                if len(applications) == 1:
                    application = applications[0]
                    all_page_applications = session.scalars(
                        select(ApplicationSnapshot).where(
                            ApplicationSnapshot.record_url.is_not(None)
                        )
                    ).all()
                    page_applications = [
                        item
                        for item in all_page_applications
                        if normalize_http_page_url(item.record_url or "")
                        == normalized_page_url
                    ]
                else:
                    page_applications = []
        except Exception:
            data = _data(
                request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                status=UpdateStatus.FAILED,
                reason_code="agent_storage_unavailable",
                reason="The Agent application store could not be read.",
            )
            return _response(
                status=UpdateStatus.FAILED,
                data=data,
                audit_id=audit_id,
                audit_persisted=False,
                error_code="agent_storage_unavailable",
                error_message=data.reason,
            )

        if application is None:
            reason_code = (
                "application_not_found"
                if not page_applications
                else "application_record_not_unique"
            )
            evidence = _evidence(
                request,
                audit_id,
                None,
                None,
                confidence=0.0,
                match_method=None,
            )
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code=reason_code,
                reason="Exactly one Agent application snapshot is required.",
                evidence=evidence,
                before=None,
                after=None,
                application=None,
                record_url=None,
                current_stage=None,
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )

        before = _application_snapshot_diff(application)
        record_url = normalize_http_page_url(application.record_url or "")
        evidence_base = _evidence(
            request,
            audit_id,
            application,
            None,
            confidence=0.0,
            match_method=None,
        )
        if record_url is None:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="record_url_missing_or_invalid",
                reason="The Agent application has no valid official record URL.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=None,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )
        if normalized_page_url != record_url:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="observation_url_mismatch",
                reason="The Edge page URL does not match the application record URL.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )
        if not application.job_title.strip():
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="target_job_invalid",
                reason="The Agent application has no target job title.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )
        if request.terminal_result.captured_at is None:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="status_captured_at_missing",
                reason="The Edge result has no timezone-aware capturedAt timestamp.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=None,
                confidence=0.0,
                match_method=None,
            )

        entries, entries_error = _normalise_entries(request.terminal_result)
        if entries_error:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code=entries_error,
                reason="The Edge result does not contain one unambiguous status evidence entry.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )

        match, match_error = _match_entry(application, page_applications, entries)
        if match is None:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code=match_error,
                reason="The Edge status evidence cannot be matched uniquely to this job.",
                evidence=evidence_base,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=None,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=None,
            )

        observed_status = _normalise_status(match.entry.status)
        if observed_status is None:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="status_evidence_unknown",
                reason="The matched Edge status is not a supported application state.",
                evidence=_evidence(
                    request,
                    audit_id,
                    application,
                    match.entry,
                    confidence=0.0,
                    match_method=match.method,
                ),
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=match.method,
            )
        if not match.entry.label.strip():
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="status_label_missing",
                reason="The matched Edge status has no human-readable label.",
                evidence=_evidence(
                    request,
                    audit_id,
                    application,
                    match.entry,
                    confidence=0.0,
                    match_method=match.method,
                ),
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=_stage_or_none(application.stage),
                target_stage=None,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=0.0,
                match_method=match.method,
            )

        confidence = _effective_confidence(request.terminal_result, match)
        evidence = _evidence(
            request,
            audit_id,
            application,
            match.entry,
            confidence=confidence,
            match_method=match.method,
        )
        current_stage = _stage_or_none(application.stage)
        if current_stage is None:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="current_stage_invalid",
                reason="The Agent application stage is not supported.",
                evidence=evidence,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=None,
                target_stage=None,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
            )
        target_stage = _target_stage(current_stage, observed_status)
        retained_historical_stage = (
            current_stage not in _TERMINAL_STAGES
            and target_stage not in _TERMINAL_STAGES
            and _STAGE_ORDER[target_stage] < _STAGE_ORDER[current_stage]
        )
        no_write_stage_confirmation = target_stage is current_stage or retained_historical_stage
        if confidence < _AUTO_CONFIDENCE and not retained_historical_stage:
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.STATE_UNCLEAR,
                reason_code="confidence_below_threshold",
                reason="The matched status evidence is below the automatic-write confidence threshold.",
                evidence=evidence,
                before=before,
                after=None,
                application=application,
                record_url=record_url,
                current_stage=current_stage,
                target_stage=None,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
            )

        after_preview = {
            "application_id": application.id,
            "record_url": record_url,
            "stage": current_stage.value if retained_historical_stage else target_stage.value,
            "source_stage": observed_status,
            "source_status": match.entry.label,
            "confidence": confidence,
            "match_method": match.method.value,
        }
        if no_write_stage_confirmation:
            audit = _new_audit(
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                evidence=evidence,
                before=before,
                after=after_preview,
                success=True,
                error_code=None,
            )
            try:
                saved, created = _insert_audit(storage, audit)
            except Exception:
                data = _data(
                    request,
                    audit_id=audit_id,
                    idempotency_key=idempotency_key,
                    status=UpdateStatus.FAILED,
                    reason_code="audit_persistence_failed",
                    reason="The unchanged status result could not be audited.",
                    application=application,
                    record_url=record_url,
                    current_stage=current_stage,
                    target_stage=current_stage,
                    entry=match.entry,
                    captured_at=request.terminal_result.captured_at,
                    confidence=confidence,
                    match_method=match.method,
                )
                return _response(
                    status=UpdateStatus.FAILED,
                    data=data,
                    audit_id=audit_id,
                    audit_persisted=False,
                    error_code="audit_persistence_failed",
                    error_message=data.reason,
                )
            if not created:
                return _response_from_audit(request, saved, idempotency_key=idempotency_key)
            data = _data(
                request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                status=UpdateStatus.UNCHANGED,
                reason_code=(
                    "historical_stage_retained" if retained_historical_stage else "unchanged"
                ),
                reason=(
                    "The observed page status is lower than the previously confirmed stage; the historical stage was retained."
                    if retained_historical_stage
                    else "The verified Edge status already matches the Agent stage."
                ),
                application=application,
                record_url=record_url,
                current_stage=current_stage,
                target_stage=current_stage,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
            )
            return _response(
                status=UpdateStatus.UNCHANGED,
                data=data,
                audit_id=audit_id,
                audit_persisted=True,
            )

        if (
            current_stage in _TERMINAL_STAGES
            or target_stage is ApplicationStage.WITHDRAWN
            or _STAGE_ORDER[target_stage] < _STAGE_ORDER[current_stage]
        ):
            reason_code = (
                "destructive_terminal_update"
                if target_stage is ApplicationStage.WITHDRAWN
                else "stage_regression_or_terminal_conflict"
            )
            return _persist_decision(
                storage,
                request=request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                token_id=token_id,
                task_id=task_id,
                started_at=started_at,
                status=UpdateStatus.APPROVAL_REQUIRED,
                reason_code=reason_code,
                reason="The requested stage is regressive or destructive and requires approval.",
                evidence=evidence,
                before=before,
                after=after_preview,
                application=application,
                record_url=record_url,
                current_stage=current_stage,
                target_stage=target_stage,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
                requires_approval=True,
            )

        payload = {
            "application_id": application.id,
            "current_stage": current_stage.value,
            "target_stage": target_stage.value,
            "source_stage": observed_status,
            "source_status": match.entry.label,
            "source_status_synced_at": request.terminal_result.captured_at.astimezone(
                timezone.utc
            ).isoformat(),
            "result": "淘汰" if target_stage is ApplicationStage.REJECTED else "进行中",
            "note": f"Edge application status verification: {match.entry.label}",
        }
        placeholder = _new_audit(
            request=request,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            token_id=token_id,
            task_id=task_id,
            started_at=started_at,
            evidence=evidence,
            before=before,
            after=after_preview,
            success=False,
            error_code="write_in_progress",
        )
        try:
            saved, created = _insert_audit(storage, placeholder)
        except Exception:
            data = _data(
                request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                status=UpdateStatus.FAILED,
                reason_code="audit_persistence_failed",
                reason="The status write could not be started because its audit record was not persisted.",
                application=application,
                record_url=record_url,
                current_stage=current_stage,
                target_stage=target_stage,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
            )
            return _response(
                status=UpdateStatus.FAILED,
                data=data,
                audit_id=audit_id,
                audit_persisted=False,
                error_code="audit_persistence_failed",
                error_message=data.reason,
            )
        if not created:
            return _response_from_audit(request, saved, idempotency_key=idempotency_key)

        try:
            write_adapter = adapter or AgentApplicationWriteAdapter(storage)
            effect = write_adapter.update_application_stage(payload)
            with storage.session() as session:
                updated_application = session.get(ApplicationSnapshot, application.id)
                if updated_application is None or updated_application.stage != target_stage.value:
                    raise RuntimeError("application write verification failed")
            effect_before = getattr(effect, "before", None) or before
            effect_after = getattr(effect, "after", None) or after_preview
            effect_after = {**effect_after, **after_preview}
            rollback = getattr(effect, "rollback_payload", None)
            final_audit = _finish_audit(
                storage,
                idempotency_key=idempotency_key,
                audit_id=audit_id,
                evidence=evidence,
                started_at=started_at,
                before=effect_before,
                after=effect_after,
                success=True,
                error_code=None,
                rollback=rollback,
            )
            if final_audit.execution_id != audit_id or not final_audit.success:
                raise RuntimeError("write audit completion conflicted")
        except Exception as exc:
            try:
                failed_audit = _finish_audit(
                    storage,
                    idempotency_key=idempotency_key,
                    audit_id=audit_id,
                    evidence=evidence,
                    started_at=started_at,
                    before=before,
                    after=after_preview,
                    success=False,
                    error_code=type(exc).__name__,
                )
                audit_persisted = failed_audit.execution_id == audit_id
            except Exception:
                audit_persisted = False
            data = _data(
                request,
                audit_id=audit_id,
                idempotency_key=idempotency_key,
                status=UpdateStatus.FAILED,
                reason_code="write_failed",
                reason="The Agent application status write failed; inspect WriteAudit.",
                application=application,
                record_url=record_url,
                current_stage=current_stage,
                target_stage=target_stage,
                entry=match.entry,
                captured_at=request.terminal_result.captured_at,
                confidence=confidence,
                match_method=match.method,
            )
            return _response(
                status=UpdateStatus.FAILED,
                data=data,
                audit_id=audit_id,
                audit_persisted=audit_persisted,
                error_code=type(exc).__name__ if audit_persisted else "audit_persistence_failed",
                error_message=data.reason,
            )

        data = _data(
            request,
            audit_id=audit_id,
            idempotency_key=idempotency_key,
            status=UpdateStatus.UPDATED,
            reason_code="updated",
            reason="The verified forward application stage was written to the Agent snapshot.",
            application=application,
            record_url=record_url,
            current_stage=current_stage,
            target_stage=target_stage,
            entry=match.entry,
            captured_at=request.terminal_result.captured_at,
            confidence=confidence,
            match_method=match.method,
            wrote=True,
        )
        return _response(
            status=UpdateStatus.UPDATED,
            data=data,
            audit_id=audit_id,
            audit_persisted=True,
        )


verify_and_update_browser_status = browser_status_update
update_application_status_from_browser = browser_status_update
BrowserStatusUpdateRequest = BrowserStatusUpdateInput


__all__ = [
    "BrowserOperationTerminalResult",
    "BrowserStatusEntry",
    "BrowserStatusUpdateData",
    "BrowserStatusUpdateInput",
    "BrowserStatusUpdateRequest",
    "BrowserStatusUpdateResponse",
    "MatchMethod",
    "UpdateStatus",
    "browser_status_update",
    "update_application_status_from_browser",
    "verify_and_update_browser_status",
]
