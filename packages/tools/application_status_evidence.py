"""Model-facing application status commit with deterministic evidence checks."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus, normalize_status
from packages.storage import ApplicationSnapshot
from packages.tools.browser_status_update import (
    BrowserStatusUpdateInput,
    BrowserStatusUpdateResponse,
    _normalise_status,
    _title_matches,
    browser_status_update,
)


class VerificationError(StrEnum):
    INTERNAL_ERROR = "verification_internal_error"
    OBSERVATION_NOT_FOUND = "observation_not_found"
    OBSERVATION_NOT_SUCCEEDED = "observation_not_succeeded"
    OBSERVATION_BINDING_MISMATCH = "observation_binding_mismatch"
    EVIDENCE_NOT_IN_OBSERVATION = "evidence_not_in_observation"
    STATUS_EVIDENCE_CONFLICT = "status_evidence_conflict"
    VISUAL_EVIDENCE_UNVERIFIED = "visual_evidence_unverified"


class VerifyApplicationStatusEvidenceInput(BaseModel):
    """Canonical status proposed by the model from one persisted observation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    application_id: str = Field(min_length=1, max_length=255)
    observation_operation_id: str = Field(min_length=1, max_length=128)
    observed_status: Literal[
        "interested", "applied", "assessment", "written", "interview", "hr", "offer",
        "rejected", "withdrawn"
    ]
    observed_label: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        return value


class VerifyApplicationStatusEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = "verify_application_status_evidence"
    success: bool
    status: str
    verification: BrowserStatusUpdateResponse | None = None
    error_code: VerificationError | str | None = None
    error_message: str | None = None
    reason_code: str | None = None
    read_only: bool = False
    retryable: bool = False


def has_unmapped_status(card: Mapping[str, object]) -> bool:
    """Retain the distinction between absent status and an unknown explicit label."""
    signals = card.get("signals") or {}
    if isinstance(signals, Mapping) and signals.get("unmapped_status"):
        return True
    if card.get("status"):
        return False
    return bool(
        card.get("label") or card.get("raw_status_labels")
        or (isinstance(signals, Mapping) and any(signals.get(key) for key in
            ("has_explicit_status", "has_active_step", "unmapped_status")))
        or re.search(r"(?:当前进度|申请进度|应聘进度|当前状态|状态|\bstatus)\s*[:：]\s*\S+",
                     str(card.get("context") or card.get("evidence") or ""), re.I)
    )


def _error(
    code: VerificationError,
    message: str,
) -> VerifyApplicationStatusEvidenceResponse:
    return VerifyApplicationStatusEvidenceResponse(
        success=False,
        status="STATE_UNCLEAR",
        error_code=code,
        error_message=message,
    )


def _structured_observation_conflicts(
    result: Mapping[str, object],
    application_id: str,
    observed_status: str,
    *,
    target_title: str = "",
    observed_label: str = "",
    evidence: str = "",
) -> bool:
    """Reject a model interpretation that disagrees with structured Edge evidence."""

    records = [item for item in (result.get("application_records") or []) if isinstance(item, Mapping)]
    title_key = lambda value: "".join(str(value or "").casefold().split())
    targets = [item for item in records if target_title and title_key(item.get("title")) == title_key(target_title)]
    if len(targets) > 1:
        return True
    if len(targets) == 1:
        card = targets[0]
        context = str(card.get("context") or card.get("evidence") or "")
        signals = card.get("signals") or {}
        if signals.get("conflicting_statuses"):
            return True
        card_status = _normalise_status(card.get("status"))
        if card_status and card_status != observed_status:
            return True
        # An unlabelled submission card can only support applied, never a later stage.
        if observed_status == "applied" and not card_status and not has_unmapped_status(card) and evidence and observed_label:
            if evidence in context and observed_label in context:
                entries = result.get("entries") or []
                other_titles = [str(item.get("title")) for item in records if item is not card]
                others_only = bool(entries) and all(
                    isinstance(entry, Mapping)
                    and entry.get("application_id", entry.get("applicationId")) in (None, "")
                    and any(_title_matches(title, str(entry.get("context") or entry.get("evidence") or ""))
                            for title in other_titles)
                    and not _title_matches(target_title, str(entry.get("context") or entry.get("evidence") or ""))
                    for entry in entries
                )
                top_status = _normalise_status(result.get("status"))
                summary_is_other_card = not top_status or top_status in {
                    _normalise_status(entry.get("status")) for entry in entries if isinstance(entry, Mapping)
                }
                if (others_only and summary_is_other_card) or (not entries and not top_status):
                    return False

    statuses: list[str] = []
    top_status = _normalise_status(result.get("status"))
    if top_status:
        statuses.append(top_status)

    raw_entries = result.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        return any(status != observed_status for status in statuses)

    entries: list[Mapping[str, object]] = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        entries.append(entry)

    matched_entries: list[Mapping[str, object]] = []
    for entry in entries:
        entry_id = entry.get("application_id")
        if entry_id is None:
            entry_id = entry.get("applicationId")
        if entry_id is not None and str(entry_id).strip() == application_id:
            matched_entries.append(entry)

    if not matched_entries:
        unbound = [
            entry for entry in entries
            if entry.get("application_id") is None and entry.get("applicationId") is None
        ]
        if len(unbound) == 1 and not target_title:
            matched_entries = unbound
        elif target_title:
            matched_entries = [
                entry for entry in unbound
                if _title_matches(
                    target_title,
                    " ".join(
                        str(entry.get(field) or "")
                        for field in ("context", "evidence", "title")
                    ),
                )
            ]
        if len(matched_entries) != 1 and (observed_label or evidence):
            matched_entries = [
                entry for entry in unbound
                if (not observed_label or observed_label in json.dumps(entry, ensure_ascii=False))
                and (not evidence or evidence in json.dumps(entry, ensure_ascii=False))
            ]
    if len(matched_entries) > 1:
        return True
    if not matched_entries:
        return True
    for entry in matched_entries:
        status = _normalise_status(entry.get("status"))
        if status:
            statuses.append(status)
    return len(set(statuses)) > 1 or any(status != observed_status for status in statuses)


def verify_application_status_evidence(
    request: VerifyApplicationStatusEvidenceInput,
    store: BrowserBridgeStore | None,
) -> VerifyApplicationStatusEvidenceResponse:
    try:
        return _verify_application_status_evidence(request, store)
    except Exception:
        logging.getLogger(__name__).exception(
            "Status verification failed for operation %s", request.observation_operation_id,
        )
        return _error(VerificationError.INTERNAL_ERROR,
                      "Status verification failed internally; do not retry unchanged evidence. Check server logs using the observation operation ID.")


def _verify_application_status_evidence(
    request: VerifyApplicationStatusEvidenceInput,
    store: BrowserBridgeStore | None,
) -> VerifyApplicationStatusEvidenceResponse:
    """Bind model interpretation to stored Edge evidence, then apply hard write rules."""

    if store is None:
        return _error(VerificationError.OBSERVATION_NOT_FOUND, "Browser bridge storage is unavailable.")
    operation = store.get_operation(request.observation_operation_id)
    if operation is None or operation.operation != OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value:
        return _error(VerificationError.OBSERVATION_NOT_FOUND, "The referenced observation does not exist.")
    if normalize_status(operation.status) is not OperationStatus.SUCCEEDED or not operation.result:
        return _error(
            VerificationError.OBSERVATION_NOT_SUCCEEDED,
            "The referenced browser observation has not completed successfully.",
        )
    command = operation.command if isinstance(operation.command, dict) else {}
    command_ids = {
        str(item).strip()
        for item in command.get("application_ids", [])
        if str(item).strip()
    } if isinstance(command.get("application_ids"), list) else set()
    primary_id = str(command.get("application_id") or "").strip()
    if primary_id:
        command_ids.add(primary_id)
    if request.application_id not in command_ids:
        return _error(
            VerificationError.OBSERVATION_BINDING_MISMATCH,
            "The observation belongs to another application.",
        )
    dom_result = {key: value for key, value in operation.result.items() if key != "vision"}
    evidence_haystack = json.dumps(dom_result, ensure_ascii=False, sort_keys=True)
    visual_reading = None
    if request.evidence not in evidence_haystack or request.observed_label not in evidence_haystack:
        visual_reading = operation.result.get("vision")
        if not isinstance(visual_reading, dict) or not any(
            event.event_type == "vision_analysis" and event.payload == visual_reading
            for event in store.get_events(operation.operation_id)
        ):
            return _error(
                VerificationError.EVIDENCE_NOT_IN_OBSERVATION,
                "No persisted server-side visual reading supports this evidence.",
            )
        evidence_haystack = str(visual_reading.get("text") or "")
    if request.evidence not in evidence_haystack:
        return _error(
            VerificationError.EVIDENCE_NOT_IN_OBSERVATION,
            "The submitted evidence is not present in the persisted Edge observation.",
        )
    if request.observed_label not in evidence_haystack:
        return _error(
            VerificationError.EVIDENCE_NOT_IN_OBSERVATION,
            "The submitted status label is not present in the persisted Edge observation.",
        )
    try:
        with store.storage.session() as session:
            application = session.get(ApplicationSnapshot, request.application_id)
            target_title = application.job_title if application is not None else ""
            current_stage = application.stage if application is not None else ""
    except Exception:
        target_title = ""
        current_stage = ""
    confidence = request.confidence
    if visual_reading is not None:
        if not target_title or not _title_matches(target_title, request.evidence):
            return _error(
                VerificationError.VISUAL_EVIDENCE_UNVERIFIED,
                "Visual evidence must quote the target job title together with its visible status.",
            )
        visual_confidence = visual_reading.get("confidence")
        if not isinstance(visual_confidence, (int, float)) or not 0 <= visual_confidence <= 1:
            return _error(VerificationError.VISUAL_EVIDENCE_UNVERIFIED, "Invalid visual confidence.")
        confidence = min(confidence, visual_confidence)
    if _structured_observation_conflicts(
        operation.result,
        request.application_id,
        request.observed_status,
        target_title=target_title,
        observed_label=request.observed_label,
        evidence=request.evidence,
    ):
        return _error(
            VerificationError.STATUS_EVIDENCE_CONFLICT,
            "The submitted status conflicts with structured evidence in the persisted Edge observation.",
        )
    captured_at = operation.result.get("captured_at")
    if not captured_at:
        return _error(
            VerificationError.OBSERVATION_BINDING_MISMATCH,
            "The persisted observation has no capture timestamp.",
        )
    page_url = str(operation.result.get("page_url") or command.get("page_url") or "")
    # Confirming an existing submission is a read, not an automatic status write.
    # Require a unique persisted target card and quotations scoped to that card.
    cards = [card for card in operation.result.get("application_records", [])
             if isinstance(card, Mapping) and "".join(str(card.get("title") or "").split())
             == "".join(target_title.split())]
    if visual_reading is None and current_stage == request.observed_status == "applied" and len(cards) == 1:
        card = cards[0]
        context = str(card.get("context") or card.get("evidence") or "")
        if (not card.get("status") and not has_unmapped_status(card)
                and not (card.get("signals") or {}).get("conflicting_statuses")
                and request.evidence in context and request.observed_label in context):
            return VerifyApplicationStatusEvidenceResponse(
                success=True, status="unchanged", reason_code="no_newer_status_observed", read_only=True,
            )
    verification = browser_status_update(
        BrowserStatusUpdateInput(
            application_id=request.application_id,
            page_url=page_url,
            operation_id=request.observation_operation_id,
            terminal_result={
                "operation_id": request.observation_operation_id,
                "idempotency_key": operation.idempotency_key,
                "status": "SUCCEEDED",
                "result": {
                    "page_url": page_url,
                    "captured_at": captured_at,
                    "entries": [
                        {
                            "application_id": request.application_id,
                            "status": request.observed_status,
                            "label": request.observed_label,
                            "context": request.evidence,
                            "evidence": request.evidence,
                            "confidence": confidence,
                        }
                    ],
                },
            },
        ),
        store.storage,
    )
    return VerifyApplicationStatusEvidenceResponse(
        success=verification.success,
        status=verification.status.value,
        verification=verification,
        error_code=verification.error_code,
        reason_code=verification.data.reason_code if verification.data else None,
        error_message=verification.error_message,
    )


__all__ = [
    "VerifyApplicationStatusEvidenceInput",
    "VerifyApplicationStatusEvidenceResponse",
    "verify_application_status_evidence",
]
