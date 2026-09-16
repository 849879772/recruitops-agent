"""Persistence for untrusted, revision-bound recruitment mail model proposals."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from packages.storage.models import utc_now

from .storage import (
    MAX_RAW_METADATA_BYTES,
    RecruitmentMailRecord,
    RecruitmentMailStore,
    _bounded_text,
    _json_bytes,
    _sanitize_json,
)


MODEL_ANALYSIS_KEY = "model_analysis"
MAX_MODEL_ANALYSIS_PAYLOAD_BYTES = MAX_RAW_METADATA_BYTES
MAX_ANALYSIS_VERSION_LENGTH = 128
MAX_ANALYSIS_OUTCOME_LENGTH = 128
MAX_MODEL_LENGTH = 128


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a JSON object")
    sanitized = _sanitize_json(payload)
    if not isinstance(sanitized, dict):
        raise ValueError("payload must be a JSON object")
    if _json_bytes(sanitized) > MAX_MODEL_ANALYSIS_PAYLOAD_BYTES:
        raise ValueError("model analysis payload exceeds 64KB")
    return sanitized


def _safe_analysis(
    *,
    content_digest: str,
    analysis_version: str,
    payload: dict[str, Any],
    outcome: str,
    model: str | None,
) -> dict[str, Any]:
    safe_digest = _bounded_text(content_digest, 64, "content_digest")
    safe_version = _bounded_text(
        analysis_version,
        MAX_ANALYSIS_VERSION_LENGTH,
        "analysis_version",
    )
    safe_outcome = _bounded_text(outcome, MAX_ANALYSIS_OUTCOME_LENGTH, "outcome")
    safe_model = _bounded_text(model, MAX_MODEL_LENGTH, "model", allow_none=True)
    assert safe_digest is not None
    assert safe_version is not None
    assert safe_outcome is not None
    return {
        "digest": safe_digest,
        "version": safe_version,
        "payload": _safe_payload(payload),
        "outcome": safe_outcome,
        "model": safe_model,
        "created_at": utc_now().isoformat(),
    }


def _same_replay(existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return all(existing.get(field) == candidate.get(field) for field in (
        "digest",
        "version",
        "payload",
        "outcome",
    ))


def save_model_analysis(
    store: RecruitmentMailStore,
    record_id: str,
    content_digest: str,
    analysis_version: str,
    payload: dict[str, Any],
    outcome: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Persist one proposed model analysis without certifying or processing the mail."""

    safe_record_id = _bounded_text(record_id, 128, "record_id")
    assert safe_record_id is not None
    candidate = _safe_analysis(
        content_digest=content_digest,
        analysis_version=analysis_version,
        payload=payload,
        outcome=outcome,
        model=model,
    )

    store._initialize()
    with store.storage.write_transaction() as session:
        record = session.scalar(
            select(RecruitmentMailRecord)
            .where(RecruitmentMailRecord.id == safe_record_id)
            .with_for_update()
        )
        if record is None:
            raise KeyError("recruitment mail record was not found")
        if record.content_digest != candidate["digest"]:
            raise ValueError("stale content_digest for recruitment mail record")

        raw_metadata = record.raw_metadata
        if not isinstance(raw_metadata, Mapping):
            raise ValueError("recruitment mail raw_metadata is invalid")
        existing = raw_metadata.get(MODEL_ANALYSIS_KEY)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise ValueError("stored model analysis is invalid")
            if _same_replay(existing, candidate):
                return deepcopy(dict(existing))
            if (existing.get("digest"), existing.get("version")) == (
                candidate["digest"], candidate["version"]
            ):
                raise ValueError("conflicting model analysis already exists")

        updated_metadata = dict(raw_metadata)
        updated_metadata[MODEL_ANALYSIS_KEY] = candidate
        record.raw_metadata = updated_metadata
        record.updated_at = utc_now()
        session.flush()
        return deepcopy(candidate)


def get_model_analysis(
    store: RecruitmentMailStore,
    record_id: str,
) -> dict[str, Any] | None:
    """Read the stored proposal, returning ``None`` for a missing record or proposal."""

    safe_record_id = _bounded_text(record_id, 128, "record_id")
    assert safe_record_id is not None
    store._initialize()
    with store.storage.session() as session:
        record = session.get(RecruitmentMailRecord, safe_record_id)
        if record is None:
            return None
        raw_metadata = record.raw_metadata
        if not isinstance(raw_metadata, Mapping):
            return None
        analysis = raw_metadata.get(MODEL_ANALYSIS_KEY)
        if analysis is None:
            return None
        if not isinstance(analysis, Mapping):
            raise ValueError("stored model analysis is invalid")
        return deepcopy(dict(analysis))


def sync_analysis_labels(store: RecruitmentMailStore, record_id: str) -> bool:
    """Project validated saved analysis into mail fields, without status writes."""
    from .analysis_binding import parsed_model_evidence
    from .model_analysis import MailTriageProposal, validate_mail_proposal
    from .models import ParsedRecruitmentEmail, RecruitmentMessageCategory

    with store.storage.write_transaction() as session:
        record = session.scalar(select(RecruitmentMailRecord).where(
            RecruitmentMailRecord.id == record_id).with_for_update())
        if record is None:
            raise KeyError("recruitment mail record was not found")
        analysis = (record.raw_metadata or {}).get(MODEL_ANALYSIS_KEY)
        if not analysis or analysis.get("digest") != record.content_digest:
            return False
        payload = analysis["payload"]
        if analysis.get("outcome") == "irrelevant":
            proposal = MailTriageProposal.model_validate(payload)
            validate_mail_proposal(proposal, record)
            if proposal.relevance.value != "irrelevant":
                raise ValueError("invalid irrelevant analysis")
            from .record_view import parsed_record
            parsed = parsed_record(record).model_copy(update={
                "category": RecruitmentMessageCategory.OTHER,
                "company_candidates": [], "job_candidates": [], "location_candidates": [],
                "time_candidates": [], "deadline_candidates": [], "link_candidates": [],
                "category_evidence": [], "pending_confirmation_reasons": [],
                "requires_confirmation": False,
            })
        else:
            parsed = parsed_model_evidence(record, payload)
        record.parsed_result = parsed.model_dump(mode="json")
        record.category = parsed.category.value
        record.pending_confirmation_reasons = parsed.pending_confirmation_reasons
        record.requires_confirmation = parsed.requires_confirmation
        record.updated_at = utc_now()
        return True


__all__ = [
    "MAX_MODEL_ANALYSIS_PAYLOAD_BYTES",
    "MODEL_ANALYSIS_KEY",
    "get_model_analysis",
    "save_model_analysis",
    "sync_analysis_labels",
]
