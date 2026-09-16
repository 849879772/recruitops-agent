from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from packages.storage.database import Storage
from packages.storage.models import Base, utc_now

from .models import (
    EmailMessage,
    MailCursor,
    ParsedRecruitmentEmail,
    RecruitmentMailProcessingStatus,
)
from .preparation import prepare_mail_for_model
from .sanitization import redact_sensitive_text


MAX_MAILBOX_LENGTH = 256
MAX_MESSAGE_ID_LENGTH = 512
MAX_THREAD_ID_LENGTH = 512
MAX_ACCOUNT_REF_LENGTH = 512
MAX_SENDER_LENGTH = 1_000
MAX_RECIPIENT_LENGTH = 1_000
MAX_RECIPIENTS = 50
MAX_SUBJECT_LENGTH = 2_000
MAX_BODY_LENGTH = 200_000
MAX_ASSOCIATION_ID_LENGTH = 255
MAX_SOURCE_LENGTH = 128
MAX_SOURCE_REF_LENGTH = 512
MAX_STATUS_LENGTH = 64
MAX_PROCESSING_ERROR_LENGTH = 512
MAX_JSON_STRING_LENGTH = 200_000
MAX_RAW_METADATA_BYTES = 64_000
MAX_PARSED_RESULT_BYTES = 1_000_000
MAX_JSON_ITEMS = 256
MAX_JSON_DEPTH = 8

_UNSET = object()
_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[\s_-]*key|authorization|cookie|"
    r"csrf|session[\s_-]*id|access[\s_-]*token|refresh[\s_-]*token)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b(?:api[\s_-]*key|access[\s_-]*token|refresh[\s_-]*token|"
    r"authorization|cookie|set-cookie|csrf[\s_-]*token|session[\s_-]*id)\b)"
    r"\s*[:=：]\s*(?:bearer\s+)?[^\s,;，；]+",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)


class RecruitmentMailRecord(Base):
    """Redacted, provider-neutral record for one locally observed email."""

    __tablename__ = "recruitment_emails"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_recruitment_emails_dedupe_key"),
        Index("ix_recruitment_emails_mailbox_message_id", "mailbox", "message_id"),
        Index(
            "ix_recruitment_emails_account_mailbox_message_id",
            "account_ref",
            "mailbox",
            "message_id",
        ),
        Index(
            "ix_recruitment_emails_uid_locator",
            "account_ref",
            "mailbox",
            "uid_validity",
            "imap_uid",
        ),
        Index("ix_recruitment_emails_received_at", "received_at"),
        Index("ix_recruitment_emails_category", "category"),
        Index("ix_recruitment_emails_processing_status", "processing_status"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_recruitment_emails_confidence",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(MAX_MAILBOX_LENGTH), nullable=False)
    message_id: Mapped[str] = mapped_column(String(MAX_MESSAGE_ID_LENGTH), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(
        String(MAX_THREAD_ID_LENGTH), nullable=True
    )
    account_ref: Mapped[str | None] = mapped_column(
        String(MAX_ACCOUNT_REF_LENGTH), nullable=True
    )
    sender: Mapped[str | None] = mapped_column(String(MAX_SENDER_LENGTH), nullable=True)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    subject: Mapped[str] = mapped_column(String(MAX_SUBJECT_LENGTH), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(String(MAX_BODY_LENGTH), default="", nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imap_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uid_validity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    parsed_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    pending_confirmation_reasons: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    safety_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    redacted_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mailbox_read: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    application_id: Mapped[str | None] = mapped_column(
        String(MAX_ASSOCIATION_ID_LENGTH), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(String(MAX_ASSOCIATION_ID_LENGTH), nullable=True)
    company_id: Mapped[str | None] = mapped_column(
        String(MAX_ASSOCIATION_ID_LENGTH), nullable=True
    )
    processing_status: Mapped[str] = mapped_column(
        String(MAX_STATUS_LENGTH), default=RecruitmentMailProcessingStatus.PENDING.value,
        server_default=RecruitmentMailProcessingStatus.PENDING.value,
        nullable=False,
    )
    processing_error: Mapped[str | None] = mapped_column(
        String(MAX_PROCESSING_ERROR_LENGTH), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(
        String(MAX_SOURCE_LENGTH), default="local", server_default="local", nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(
        String(MAX_SOURCE_REF_LENGTH), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    @property
    def status(self) -> str:
        return self.processing_status

    @status.setter
    def status(self, value: str) -> None:
        self.processing_status = value

    @property
    def parsed(self) -> dict[str, Any]:
        return self.parsed_result

    @property
    def message_digest(self) -> str:
        return self.content_digest

    @property
    def mailbox_seen(self) -> bool | None:
        return self.mailbox_read


RecruitmentEmailRecord = RecruitmentMailRecord
RecruitmentMail = RecruitmentMailRecord


class RecruitmentMailCursorRecord(Base):
    """Durable provider cursor; the key is a local, non-secret account reference."""

    __tablename__ = "recruitment_mail_cursors"

    account_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(MAX_MAILBOX_LENGTH), primary_key=True)
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    uid_validity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class RecruitmentMailSyncRunRecord(Base):
    """Durable receipt for one mailbox synchronization operation."""

    __tablename__ = "recruitment_mail_sync_runs"
    __table_args__ = (
        Index("ix_recruitment_mail_sync_runs_run_id", "run_id"),
        Index("ix_recruitment_mail_sync_runs_started_at", "started_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(MAX_MAILBOX_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched: Mapped[int] = mapped_column(default=0, nullable=False)
    inserted: Mapped[int] = mapped_column(default=0, nullable=False)
    reused: Mapped[int] = mapped_column(default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(default=1, nullable=False)
    cursor_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(MAX_PROCESSING_ERROR_LENGTH), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecruitmentMailSyncItemRecord(Base):
    """Auditable disposition of one deduplicated mail record in one sync."""

    __tablename__ = "recruitment_mail_sync_items"
    __table_args__ = (
        UniqueConstraint("operation_id", "mail_record_id", name="uq_mail_sync_item"),
        Index("ix_recruitment_mail_sync_items_operation_id", "operation_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mail_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(MAX_SOURCE_REF_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


def _redact_text(value: str) -> str:
    value = redact_sensitive_text(value)
    value = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('label')}=[REDACTED:secret]", value
    )
    return _BEARER_TOKEN_RE.sub("Bearer [REDACTED:token]", value)


def _bounded_text(value: Any, maximum: int, field: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if not isinstance(value, str):
        value = str(value)
    value = _redact_text(value)
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _sanitize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("JSON payload exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON payload contains a non-finite number")
        return value
    if isinstance(value, str):
        return _bounded_text(value, MAX_JSON_STRING_LENGTH, "JSON string")
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError("JSON object exceeds the maximum item count")
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _bounded_text(raw_key, 128, "JSON key")
            assert key is not None
            result[key] = "[REDACTED:secret]" if _SENSITIVE_KEY_RE.search(key) else _sanitize_json(
                raw_value, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError("JSON array exceeds the maximum item count")
        return [_sanitize_json(item, depth=depth + 1) for item in value]
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    )


def _safe_string_list(value: Any, *, maximum_items: int, maximum_length: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a JSON array")
    if len(value) > maximum_items:
        raise ValueError(f"{field} exceeds {maximum_items} items")
    return [
        _bounded_text(item, maximum_length, f"{field}[{index}]")  # type: ignore[misc]
        for index, item in enumerate(value)
    ]


def _safe_identifier(value: Any, field: str) -> str | None:
    return _bounded_text(value, MAX_ASSOCIATION_ID_LENGTH, field, allow_none=True)


def _identity_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _has_redaction_marker(value: str) -> bool:
    return "[REDACTED:" in value


def _is_explicit_identity(value: str) -> bool:
    return bool(value) and value != "local-message" and not value.startswith("local-")


def _stored_message_id_hash(record: RecruitmentMailRecord) -> str | None:
    metadata = record.raw_metadata
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("message_id_hash")
    if isinstance(value, str):
        return value
    transport = metadata.get("transport")
    if isinstance(transport, Mapping):
        value = transport.get("message_id_hash")
        if isinstance(value, str):
            return value
    return None


def _merge_existing_transport(
    existing_raw_metadata: Any,
    incoming_raw_metadata: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(incoming_raw_metadata)
    if not isinstance(existing_raw_metadata, Mapping):
        return merged
    existing_transport = existing_raw_metadata.get("transport")
    if not isinstance(existing_transport, Mapping):
        return merged
    incoming_transport = merged.get("transport")
    transport = dict(existing_transport)
    if isinstance(incoming_transport, Mapping):
        transport.update(incoming_transport)
    merged["transport"] = transport
    return merged


def _as_email_message(value: Any) -> EmailMessage:
    if isinstance(value, EmailMessage):
        return value
    if isinstance(value, Mapping):
        return EmailMessage.model_validate(value)
    if isinstance(value, str):
        return EmailMessage(body_text=value)
    if value is None:
        return EmailMessage()
    raise TypeError("message must be EmailMessage, mapping, string, or None")


def _message_from_parsed(parsed: ParsedRecruitmentEmail) -> EmailMessage:
    return EmailMessage(
        identity=parsed.identity,
        sender=parsed.sender,
        recipients=list(parsed.recipients),
        subject=parsed.subject,
        body_text=parsed.body_text,
        received_at=parsed.received_at,
    )


def _is_parsed_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and "category" in value and "identity" in value


def _coalesce_status(
    processing_status: str | RecruitmentMailProcessingStatus | None,
    status: str | RecruitmentMailProcessingStatus | None,
) -> str | None:
    if processing_status is not None and status is not None:
        raise TypeError("processing_status and status cannot both be provided")
    value = processing_status if processing_status is not None else status
    if value is None:
        return None
    return _bounded_text(getattr(value, "value", value), MAX_STATUS_LENGTH, "processing_status")


def _date_or_datetime_filter(column: Any, value: date | datetime, *, lower: bool) -> Any:
    if isinstance(value, datetime):
        return column >= value if lower else column <= value
    expression = func.date(column)
    return expression >= value.isoformat() if lower else expression <= value.isoformat()


class RecruitmentMailStore:
    """Persist and query redacted recruitment mail in the Agent-owned database."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self._initialized = False
        self._initialize()

    def _initialize(self) -> None:
        if not self._initialized:
            self.storage.initialize()
            self._initialized = True

    def upsert(
        self,
        message: EmailMessage | Mapping[str, Any] | str | ParsedRecruitmentEmail | None = None,
        parsed: ParsedRecruitmentEmail | Mapping[str, Any] | None = None,
        *,
        parsed_result: ParsedRecruitmentEmail | Mapping[str, Any] | None = None,
        processing_status: str | RecruitmentMailProcessingStatus | None = None,
        status: str | RecruitmentMailProcessingStatus | None = None,
        application_id: str | None | object = _UNSET,
        job_id: str | None | object = _UNSET,
        company_id: str | None | object = _UNSET,
        processing_error: str | None | object = _UNSET,
        processed_at: datetime | None | object = _UNSET,
        source: str = "local",
        source_ref: str | None | object = _UNSET,
    ) -> RecruitmentMailRecord:
        """Sanitize and idempotently save mail without inferring semantic fields."""

        if parsed is not None and parsed_result is not None:
            raise TypeError("parsed and parsed_result cannot both be provided")
        parsed_input = parsed if parsed is not None else parsed_result

        if isinstance(message, ParsedRecruitmentEmail):
            if parsed_input is not None:
                raise TypeError("a parsed result was provided twice")
            email_source = _message_from_parsed(message)
            parsed_model = message
        elif parsed_input is None and _is_parsed_mapping(message):
            parsed_model = ParsedRecruitmentEmail.model_validate(message)
            email_source = _message_from_parsed(parsed_model)
        else:
            email_source = _as_email_message(message)
            if parsed_input is None:
                parsed_model = prepare_mail_for_model(email_source)
            elif isinstance(parsed_input, ParsedRecruitmentEmail):
                parsed_model = parsed_input
            else:
                parsed_model = ParsedRecruitmentEmail.model_validate(parsed_input)

        identity_source = email_source.identity
        if (
            not _is_explicit_identity(identity_source.message_id)
            and _is_explicit_identity(parsed_model.identity.message_id)
        ):
            identity_source = parsed_model.identity
        original_message_id = identity_source.message_id
        original_mailbox = identity_source.mailbox
        original_account_ref = identity_source.account_ref

        payload = _sanitize_json(parsed_model.model_dump(mode="json"))
        assert isinstance(payload, dict)
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("parsed result identity is required")

        mailbox = _bounded_text(identity.get("mailbox", "INBOX"), MAX_MAILBOX_LENGTH, "mailbox")
        message_id = _bounded_text(
            identity.get("message_id"), MAX_MESSAGE_ID_LENGTH, "message_id"
        )
        thread_id = _bounded_text(
            identity.get("thread_id"), MAX_THREAD_ID_LENGTH, "thread_id", allow_none=True
        )
        account_ref = _bounded_text(
            identity.get("account_ref"), MAX_ACCOUNT_REF_LENGTH, "account_ref", allow_none=True
        )
        sender = _bounded_text(
            payload.get("sender"), MAX_SENDER_LENGTH, "sender", allow_none=True
        )
        recipients = _safe_string_list(
            payload.get("recipients", []),
            maximum_items=MAX_RECIPIENTS,
            maximum_length=MAX_RECIPIENT_LENGTH,
            field="recipients",
        )
        subject = _bounded_text(
            payload.get("subject", ""), MAX_SUBJECT_LENGTH, "subject"
        )
        body_text = _bounded_text(
            payload.get("body_text", ""), MAX_BODY_LENGTH, "body_text"
        )
        category = _bounded_text(payload.get("category"), 64, "category")
        confidence = float(parsed_model.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

        pending_reasons = _safe_string_list(
            payload.get("pending_confirmation_reasons", []),
            maximum_items=30,
            maximum_length=256,
            field="pending_confirmation_reasons",
        )
        safety_flags = _safe_string_list(
            payload.get("safety_flags", []),
            maximum_items=30,
            maximum_length=256,
            field="safety_flags",
        )
        redacted_fields = _safe_string_list(
            payload.get("redacted_fields", []),
            maximum_items=30,
            maximum_length=128,
            field="redacted_fields",
        )
        category_evidence = _safe_string_list(
            payload.get("category_evidence", []),
            maximum_items=20,
            maximum_length=1_000,
            field="category_evidence",
        )
        payload["identity"] = {
            "message_id": message_id,
            "thread_id": thread_id,
            "mailbox": mailbox,
            "account_ref": account_ref,
        }
        payload["sender"] = sender
        payload["recipients"] = recipients
        payload["subject"] = subject
        payload["body_text"] = body_text
        payload["pending_confirmation_reasons"] = pending_reasons
        payload["safety_flags"] = safety_flags
        payload["redacted_fields"] = redacted_fields
        payload["category_evidence"] = category_evidence
        if _json_bytes(payload) > MAX_PARSED_RESULT_BYTES:
            raise ValueError(f"parsed_result exceeds {MAX_PARSED_RESULT_BYTES} bytes")

        digest_payload = {
            "sender": sender,
            "recipients": recipients,
            "subject": subject,
            "body_text": body_text,
        }
        content_digest = sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        explicit_message_id = _is_explicit_identity(original_message_id)
        original_message_id_hash = (
            _identity_hash(original_message_id) if explicit_message_id else None
        )
        if explicit_message_id:
            dedupe_kind = "message_id"
            dedupe_basis = json.dumps(
                {
                    "account_ref": _identity_hash(original_account_ref or ""),
                    "mailbox": _identity_hash(original_mailbox),
                    "message_id": original_message_id_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            dedupe_kind = "content"
            dedupe_basis = content_digest
        dedupe_key = f"{dedupe_kind}:{sha256(dedupe_basis.encode('utf-8')).hexdigest()}"
        record_id = f"mail-{sha256(dedupe_key.encode('utf-8')).hexdigest()[:32]}"

        received_at = parsed_model.received_at or email_source.received_at
        safe_source = _bounded_text(source, MAX_SOURCE_LENGTH, "source")
        assert safe_source is not None
        safe_source_ref = (
            _bounded_text(source_ref, MAX_SOURCE_REF_LENGTH, "source_ref", allow_none=True)
            if source_ref is not _UNSET
            else None
        )
        raw_metadata = {
            "message_id": message_id,
            "thread_id": thread_id,
            "mailbox": mailbox,
            "account_ref": account_ref,
            "sender": sender,
            "recipients": recipients,
            "subject": subject,
            "received_at": received_at.isoformat() if received_at is not None else None,
            "has_html_body": email_source.html_body is not None,
            "source": safe_source,
            "source_ref": safe_source_ref,
        }
        if original_message_id_hash is not None:
            raw_metadata["message_id_hash"] = original_message_id_hash
        source_metadata = getattr(email_source, "source_metadata", None)
        if isinstance(source_metadata, dict) and source_metadata:
            transport_metadata = _sanitize_json(source_metadata)
            if isinstance(transport_metadata, dict) and "message_id" in transport_metadata:
                transport_metadata["message_id"] = message_id
            raw_metadata["transport"] = transport_metadata
        if _json_bytes(raw_metadata) > MAX_RAW_METADATA_BYTES:
            raise ValueError(f"raw_metadata exceeds {MAX_RAW_METADATA_BYTES} bytes")

        requested_status = _coalesce_status(processing_status, status)
        association_values = {
            field: _safe_identifier(value, field)
            for field, value in (
                ("application_id", application_id),
                ("job_id", job_id),
                ("company_id", company_id),
            )
            if value is not _UNSET
        }
        values: dict[str, Any] = {
            "id": record_id,
            "dedupe_key": dedupe_key,
            "dedupe_kind": dedupe_kind,
            "mailbox": mailbox,
            "message_id": message_id,
            "thread_id": thread_id,
            "account_ref": account_ref,
            "sender": sender,
            "recipients": recipients,
            "subject": subject,
            "body_text": body_text,
            "received_at": received_at,
            "imap_uid": (source_metadata or {}).get("imap_uid") if isinstance(source_metadata, dict) else None,
            "uid_validity": (source_metadata or {}).get("uid_validity") if isinstance(source_metadata, dict) else None,
            "content_digest": content_digest,
            "raw_metadata": raw_metadata,
            "parsed_result": payload,
            "category": category,
            "confidence": confidence,
            "pending_confirmation_reasons": pending_reasons,
            "safety_flags": safety_flags,
            "redacted_fields": redacted_fields,
            "requires_confirmation": bool(parsed_model.requires_confirmation),
            "mailbox_read": getattr(email_source, "mailbox_read", None),
            "processing_status": requested_status or RecruitmentMailProcessingStatus.PENDING.value,
            "processing_error": (
                _bounded_text(
                    processing_error,
                    MAX_PROCESSING_ERROR_LENGTH,
                    "processing_error",
                    allow_none=True,
                )
                if processing_error is not _UNSET
                else None
            ),
            "processed_at": (
                processed_at
                if processed_at is not _UNSET
                else (
                    utc_now()
                    if requested_status in {
                        RecruitmentMailProcessingStatus.PROCESSED.value,
                        RecruitmentMailProcessingStatus.LINKED.value,
                        RecruitmentMailProcessingStatus.IGNORED.value,
                        RecruitmentMailProcessingStatus.PROCESSED_UPDATED.value,
                        RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED.value,
                        RecruitmentMailProcessingStatus.IRRELEVANT.value,
                        RecruitmentMailProcessingStatus.PENDING_ASSOCIATION.value,
                        RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA.value,
                        RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value,
                        RecruitmentMailProcessingStatus.FAILED_TERMINAL.value,
                    }
                    else None
                )
            ),
            "source": safe_source,
            "source_ref": safe_source_ref,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            **association_values,
        }
        update_columns = {
            key
            for key in values
            if key not in {"id", "dedupe_key", "dedupe_kind", "created_at", "processing_status"}
        }
        if requested_status is not None:
            update_columns.add("processing_status")

        self._initialize()
        with self.storage.write_transaction() as session:
            lookup_dedupe_key = dedupe_key
            existing_record = None
            if explicit_message_id:
                legacy_record = self._find_message_record(
                    session,
                    mailbox=mailbox,
                    message_id=original_message_id,
                    account_ref=account_ref,
                )
                same_identity = (
                    legacy_record is not None
                    and (
                        _stored_message_id_hash(legacy_record) == original_message_id_hash
                        or (
                            original_message_id == message_id
                            and legacy_record.message_id == message_id
                            and not _has_redaction_marker(message_id)
                        )
                    )
                )
                if same_identity:
                    existing_record = legacy_record
                if (
                    same_identity
                    and legacy_record.dedupe_key != dedupe_key
                ):
                    # Keep the legacy key in place; this reuses known old data without a migration.
                    values["id"] = legacy_record.id
                    values["dedupe_key"] = legacy_record.dedupe_key
                    lookup_dedupe_key = legacy_record.dedupe_key
            if existing_record is None:
                existing_record = session.scalar(
                    select(RecruitmentMailRecord).where(
                        RecruitmentMailRecord.dedupe_key == lookup_dedupe_key
                    )
                )
            if existing_record is not None:
                values["raw_metadata"] = _merge_existing_transport(
                    existing_record.raw_metadata,
                    values["raw_metadata"],
                )
                if not (
                    isinstance(source_metadata, dict)
                    and source_metadata.get("imap_uid") is not None
                ):
                    values["imap_uid"] = existing_record.imap_uid
                if not (
                    isinstance(source_metadata, dict)
                    and source_metadata.get("uid_validity") is not None
                ):
                    values["uid_validity"] = existing_record.uid_validity
                if getattr(email_source, "mailbox_read", None) is None:
                    values["mailbox_read"] = existing_record.mailbox_read
            self._upsert(session, values, update_columns)
            record = session.scalar(
                select(RecruitmentMailRecord)
                .where(RecruitmentMailRecord.dedupe_key == lookup_dedupe_key)
                .execution_options(populate_existing=True)
            )
            if record is None:
                raise RuntimeError("recruitment mail upsert did not return a record")
            session.flush()
            return record

    save = upsert
    upsert_email = upsert
    save_email = upsert

    def start_sync_run(
        self,
        *,
        run_id: str,
        operation_id: str,
        account_key: str,
        mailbox: str,
        attempts: int,
        cursor_before: str | None,
    ) -> RecruitmentMailSyncRunRecord:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        values = {
            "operation_id": _bounded_text(operation_id, 128, "operation_id"),
            "run_id": _bounded_text(run_id, 128, "run_id"),
            "account_key": _bounded_text(account_key, 128, "account_key"),
            "mailbox": _bounded_text(mailbox, MAX_MAILBOX_LENGTH, "mailbox"),
            "status": "running",
            "fetched": 0,
            "inserted": 0,
            "reused": 0,
            "attempts": attempts,
            "cursor_before": _bounded_text(
                cursor_before, 1024, "cursor_before", allow_none=True
            ),
            "cursor": None,
            "error": None,
            "started_at": utc_now(),
            "finished_at": None,
        }
        table = RecruitmentMailSyncRunRecord.__table__
        self._initialize()
        with self.storage.write_transaction() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(
                    sqlite_insert(table).values(**values).on_conflict_do_update(
                        index_elements=["operation_id"], set_=values
                    )
                )
            elif dialect == "postgresql":
                session.execute(
                    postgresql_insert(table).values(**values).on_conflict_do_update(
                        index_elements=["operation_id"], set_=values
                    )
                )
            else:
                existing = session.get(RecruitmentMailSyncRunRecord, values["operation_id"])
                if existing is None:
                    session.add(RecruitmentMailSyncRunRecord(**values))
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
            session.flush()
            record = session.get(RecruitmentMailSyncRunRecord, values["operation_id"])
            if record is None:
                raise RuntimeError("mail sync run did not persist")
            return record

    def record_sync_item(
        self,
        *,
        operation_id: str,
        run_id: str,
        mail_record_id: str,
        disposition: str,
    ) -> RecruitmentMailSyncItemRecord:
        if disposition not in {"inserted", "reused"}:
            raise ValueError("disposition must be inserted or reused")
        safe_operation_id = _bounded_text(operation_id, 128, "operation_id")
        safe_mail_record_id = _bounded_text(mail_record_id, 128, "mail_record_id")
        item_id = (
            "mail-sync-item-"
            + sha256(f"{safe_operation_id}\0{safe_mail_record_id}".encode()).hexdigest()[:32]
        )
        values = {
            "id": item_id,
            "operation_id": safe_operation_id,
            "run_id": _bounded_text(run_id, 128, "run_id"),
            "mail_record_id": safe_mail_record_id,
            "disposition": disposition,
            "evidence_ref": f"recruitment_mail:{safe_mail_record_id}",
            "created_at": utc_now(),
        }
        table = RecruitmentMailSyncItemRecord.__table__
        self._initialize()
        with self.storage.write_transaction() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(sqlite_insert(table).values(**values).on_conflict_do_nothing())
            elif dialect == "postgresql":
                session.execute(postgresql_insert(table).values(**values).on_conflict_do_nothing())
            elif session.get(RecruitmentMailSyncItemRecord, item_id) is None:
                session.add(RecruitmentMailSyncItemRecord(**values))
            session.flush()
            record = session.get(RecruitmentMailSyncItemRecord, item_id)
            if record is None:
                raise RuntimeError("mail sync item did not persist")
            return record

    def finish_sync_run(
        self,
        operation_id: str,
        *,
        status: str,
        fetched: int,
        inserted: int,
        reused: int,
        cursor: str | None,
        error: str | None = None,
    ) -> RecruitmentMailSyncRunRecord:
        if status not in {"succeeded", "failed"}:
            raise ValueError("sync status must be succeeded or failed")
        safe_operation_id = _bounded_text(operation_id, 128, "operation_id")
        safe_error = _bounded_text(
            error, MAX_PROCESSING_ERROR_LENGTH, "error", allow_none=True
        )
        self._initialize()
        with self.storage.write_transaction() as session:
            record = session.get(RecruitmentMailSyncRunRecord, safe_operation_id)
            if record is None:
                raise KeyError(f"mail sync operation not found: {safe_operation_id}")
            record.status = status
            record.fetched = fetched
            record.inserted = inserted
            record.reused = reused
            record.cursor = _bounded_text(cursor, 1024, "cursor", allow_none=True)
            record.error = safe_error
            record.finished_at = utc_now()
            session.flush()
            return record

    def get_sync_run(self, operation_id: str) -> RecruitmentMailSyncRunRecord | None:
        safe_operation_id = _bounded_text(operation_id, 128, "operation_id")
        self._initialize()
        with self.storage.session() as session:
            return session.get(RecruitmentMailSyncRunRecord, safe_operation_id)

    def list_sync_items(self, operation_id: str) -> list[RecruitmentMailSyncItemRecord]:
        safe_operation_id = _bounded_text(operation_id, 128, "operation_id")
        self._initialize()
        with self.storage.session() as session:
            return list(
                session.scalars(
                    select(RecruitmentMailSyncItemRecord)
                    .where(RecruitmentMailSyncItemRecord.operation_id == safe_operation_id)
                    .order_by(
                        RecruitmentMailSyncItemRecord.created_at,
                        RecruitmentMailSyncItemRecord.id,
                    )
                )
            )

    def get_cursor(self, account_key: str, mailbox: str = "INBOX") -> MailCursor:
        safe_account = _bounded_text(account_key, 128, "account_key")
        safe_mailbox = _bounded_text(mailbox, MAX_MAILBOX_LENGTH, "mailbox")
        self._initialize()
        with self.storage.session() as session:
            record = session.get(
                RecruitmentMailCursorRecord,
                {"account_key": safe_account, "mailbox": safe_mailbox},
            )
            return MailCursor(
                mailbox=safe_mailbox,
                token=record.token if record is not None else None,
                updated_at=record.updated_at if record is not None else None,
            )

    def save_cursor(self, account_key: str, cursor: MailCursor) -> MailCursor:
        safe_account = _bounded_text(account_key, 128, "account_key")
        safe_mailbox = _bounded_text(cursor.mailbox, MAX_MAILBOX_LENGTH, "mailbox")
        safe_token = _bounded_text(cursor.token, 1024, "cursor token", allow_none=True)
        now = utc_now()
        values = {
            "account_key": safe_account,
            "mailbox": safe_mailbox,
            "token": safe_token,
            "updated_at": now,
        }
        table = RecruitmentMailCursorRecord.__table__
        self._initialize()
        with self.storage.write_transaction() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(
                    sqlite_insert(table).values(**values).on_conflict_do_update(
                        index_elements=["account_key", "mailbox"],
                        set_={"token": safe_token, "updated_at": now},
                    )
                )
            elif dialect == "postgresql":
                session.execute(
                    postgresql_insert(table).values(**values).on_conflict_do_update(
                        index_elements=["account_key", "mailbox"],
                        set_={"token": safe_token, "updated_at": now},
                    )
                )
            else:
                record = session.get(
                    RecruitmentMailCursorRecord,
                    {"account_key": safe_account, "mailbox": safe_mailbox},
                )
                if record is None:
                    session.add(RecruitmentMailCursorRecord(**values))
                else:
                    record.token = safe_token
                    record.updated_at = now
        return MailCursor(mailbox=safe_mailbox, token=safe_token, updated_at=now)

    @staticmethod
    def _upsert(session: Any, values: dict[str, Any], update_columns: set[str]) -> None:
        table = RecruitmentMailRecord.__table__
        update_values = {key: values[key] for key in update_columns}
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(table).values(**values).on_conflict_do_update(
                index_elements=["dedupe_key"], set_=update_values
            )
            session.execute(statement)
            return
        if dialect == "postgresql":
            statement = postgresql_insert(table).values(**values).on_conflict_do_update(
                index_elements=["dedupe_key"], set_=update_values
            )
            session.execute(statement)
            return

        predicates = [table.c.dedupe_key == values["dedupe_key"]]
        if session.execute(select(table.c.id).where(and_(*predicates))).first():
            session.execute(update(table).where(and_(*predicates)).values(**update_values))
        else:
            session.execute(insert(table).values(**values))

    @staticmethod
    def _find_message_record(
        session: Any,
        *,
        mailbox: str,
        message_id: str,
        account_ref: str | None = None,
    ) -> RecruitmentMailRecord | None:
        predicates = [
            RecruitmentMailRecord.mailbox
            == _bounded_text(mailbox, MAX_MAILBOX_LENGTH, "mailbox"),
        ]
        if account_ref is not None:
            predicates.append(
                RecruitmentMailRecord.account_ref
                == _bounded_text(account_ref, MAX_ACCOUNT_REF_LENGTH, "account_ref")
            )
        records = list(
            session.scalars(
                select(RecruitmentMailRecord).where(and_(*predicates))
            )
        )
        expected_hash = (
            _identity_hash(message_id) if _is_explicit_identity(message_id) else None
        )
        if expected_hash is not None:
            for record in records:
                if _stored_message_id_hash(record) == expected_hash:
                    return record

        safe_message_id = _bounded_text(
            message_id, MAX_MESSAGE_ID_LENGTH, "message_id"
        )
        for record in records:
            if _stored_message_id_hash(record) is None and record.message_id == safe_message_id:
                return record
        return None

    def get(
        self,
        record_id: str | None = None,
        message_id: str | None = None,
        *,
        mailbox: str | None = None,
        account_ref: str | None = None,
        content_digest: str | None = None,
        dedupe_key: str | None = None,
    ) -> RecruitmentMailRecord | None:
        self._initialize()
        with self.storage.session() as session:
            if message_id is not None:
                if mailbox is None:
                    if record_id is None:
                        raise TypeError("mailbox is required when looking up a message_id")
                    mailbox, record_id = record_id, None
                return self._find_message_record(
                    session,
                    mailbox=mailbox,
                    message_id=message_id,
                    account_ref=account_ref,
                )
            elif content_digest is not None:
                statement = select(RecruitmentMailRecord).where(
                    RecruitmentMailRecord.content_digest == _bounded_text(
                        content_digest, 64, "content_digest"
                    )
                )
            elif dedupe_key is not None:
                statement = select(RecruitmentMailRecord).where(
                    RecruitmentMailRecord.dedupe_key == _bounded_text(
                        dedupe_key, 128, "dedupe_key"
                    )
                )
            elif record_id is not None:
                statement = select(RecruitmentMailRecord).where(
                    RecruitmentMailRecord.id == _bounded_text(record_id, 128, "record_id")
                )
            else:
                raise TypeError("one mail lookup key is required")
            return session.scalar(statement)

    def get_by_mailbox_message_id(
        self,
        mailbox: str,
        message_id: str,
        *,
        account_ref: str | None = None,
    ) -> RecruitmentMailRecord | None:
        return self.get(
            message_id=message_id,
            mailbox=mailbox,
            account_ref=account_ref,
        )

    def get_by_content_digest(self, content_digest: str) -> RecruitmentMailRecord | None:
        return self.get(content_digest=content_digest)

    def get_by_dedupe_key(self, dedupe_key: str) -> RecruitmentMailRecord | None:
        return self.get(dedupe_key=dedupe_key)

    def query(
        self,
        *,
        received_from: date | datetime | None = None,
        received_to: date | datetime | None = None,
        start_date: date | datetime | None = None,
        end_date: date | datetime | None = None,
        category: str | Any | None = None,
        categories: list[str | Any] | tuple[str | Any, ...] | None = None,
        processing_status: str | RecruitmentMailProcessingStatus | None = None,
        status: str | RecruitmentMailProcessingStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RecruitmentMailRecord]:
        if received_from is not None and start_date is not None:
            raise TypeError("received_from and start_date cannot both be provided")
        if received_to is not None and end_date is not None:
            raise TypeError("received_to and end_date cannot both be provided")
        received_from = received_from if received_from is not None else start_date
        received_to = received_to if received_to is not None else end_date
        if not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be non-negative")
        if processing_status is not None and status is not None:
            raise TypeError("processing_status and status cannot both be provided")
        if category is not None and categories is not None:
            raise TypeError("category and categories cannot both be provided")
        status_value = _coalesce_status(processing_status, status)
        category_value = (
            _bounded_text(getattr(category, "value", category), 64, "category")
            if category is not None
            else None
        )
        category_values = (
            [
                _bounded_text(getattr(item, "value", item), 64, "category")
                for item in categories
            ]
            if categories is not None
            else None
        )

        self._initialize()
        with self.storage.session() as session:
            statement = select(RecruitmentMailRecord)
            if received_from is not None:
                statement = statement.where(
                    _date_or_datetime_filter(RecruitmentMailRecord.received_at, received_from, lower=True)
                )
            if received_to is not None:
                statement = statement.where(
                    _date_or_datetime_filter(RecruitmentMailRecord.received_at, received_to, lower=False)
                )
            if category_value is not None:
                statement = statement.where(RecruitmentMailRecord.category == category_value)
            if category_values is not None:
                statement = statement.where(RecruitmentMailRecord.category.in_(category_values))
            if status_value is not None:
                statement = statement.where(
                    RecruitmentMailRecord.processing_status == status_value
                )
            statement = (
                statement.order_by(
                    RecruitmentMailRecord.received_at.desc(), RecruitmentMailRecord.id.desc()
                )
                .offset(offset)
                .limit(limit)
            )
            return list(session.scalars(statement))

    def count(
        self,
        *,
        received_from: date | datetime | None = None,
        received_to: date | datetime | None = None,
        start_date: date | datetime | None = None,
        end_date: date | datetime | None = None,
        category: str | Any | None = None,
        categories: list[str | Any] | tuple[str | Any, ...] | None = None,
        processing_status: str | RecruitmentMailProcessingStatus | None = None,
        status: str | RecruitmentMailProcessingStatus | None = None,
    ) -> int:
        if received_from is not None and start_date is not None:
            raise TypeError("received_from and start_date cannot both be provided")
        if received_to is not None and end_date is not None:
            raise TypeError("received_to and end_date cannot both be provided")
        if processing_status is not None and status is not None:
            raise TypeError("processing_status and status cannot both be provided")
        if category is not None and categories is not None:
            raise TypeError("category and categories cannot both be provided")
        received_from = received_from if received_from is not None else start_date
        received_to = received_to if received_to is not None else end_date
        status_value = _coalesce_status(processing_status, status)
        category_value = (
            _bounded_text(getattr(category, "value", category), 64, "category")
            if category is not None
            else None
        )
        category_values = (
            [
                _bounded_text(getattr(item, "value", item), 64, "category")
                for item in categories
            ]
            if categories is not None
            else None
        )

        self._initialize()
        with self.storage.session() as session:
            statement = select(func.count()).select_from(RecruitmentMailRecord)
            if received_from is not None:
                statement = statement.where(
                    _date_or_datetime_filter(RecruitmentMailRecord.received_at, received_from, lower=True)
                )
            if received_to is not None:
                statement = statement.where(
                    _date_or_datetime_filter(RecruitmentMailRecord.received_at, received_to, lower=False)
                )
            if category_value is not None:
                statement = statement.where(RecruitmentMailRecord.category == category_value)
            if category_values is not None:
                statement = statement.where(RecruitmentMailRecord.category.in_(category_values))
            if status_value is not None:
                statement = statement.where(
                    RecruitmentMailRecord.processing_status == status_value
                )
            return int(session.scalar(statement) or 0)

    list_emails = query
    search = query

    def list_by_date(self, on_date: date, *, limit: int = 100, offset: int = 0) -> list[RecruitmentMailRecord]:
        return self.query(start_date=on_date, end_date=on_date, limit=limit, offset=offset)

    def update_associations(
        self,
        record_id: str | None = None,
        message_id: str | None = None,
        *,
        mailbox: str | None = None,
        account_ref: str | None = None,
        dedupe_key: str | None = None,
        application_id: str | None | object = _UNSET,
        job_id: str | None | object = _UNSET,
        company_id: str | None | object = _UNSET,
    ) -> RecruitmentMailRecord:
        values = {
            field: _safe_identifier(value, field)
            for field, value in (
                ("application_id", application_id),
                ("job_id", job_id),
                ("company_id", company_id),
            )
            if value is not _UNSET
        }
        if not values:
            raise ValueError("at least one association field is required")
        self._initialize()
        with self.storage.write_transaction() as session:
            record = self._record_in_session(
                session,
                record_id=record_id,
                message_id=message_id,
                mailbox=mailbox,
                account_ref=account_ref,
                dedupe_key=dedupe_key,
            )
            if record is None:
                raise KeyError("recruitment mail record was not found")
            for field, value in values.items():
                setattr(record, field, value)
            record.updated_at = utc_now()
            session.flush()
            return record

    update_links = update_associations

    def update_source_metadata(
        self,
        record_id: str,
        source_metadata: Mapping[str, Any],
        *,
        processing_status: str | RecruitmentMailProcessingStatus | None = None,
        processing_error: str | None = None,
        backfill_outcome: str | None = None,
    ) -> RecruitmentMailRecord:
        """Persist verified connector metadata without reparsing or duplicating mail."""

        safe_metadata = _sanitize_json(dict(source_metadata))
        if not isinstance(safe_metadata, dict):
            raise ValueError("source metadata must be an object")
        self._initialize()
        with self.storage.write_transaction() as session:
            record = self._record_in_session(session, record_id=record_id)
            if record is None:
                raise KeyError("recruitment mail record was not found")
            raw_metadata = dict(record.raw_metadata or {})
            raw_metadata["transport"] = safe_metadata
            if safe_metadata.get("message_id_hash"):
                raw_metadata["message_id_hash"] = safe_metadata["message_id_hash"]
            if backfill_outcome is not None:
                from .authentication import AUTHENTICATION_VERSION
                raw_metadata["authentication_backfill"] = {
                    "version": AUTHENTICATION_VERSION,
                    "outcome": _bounded_text(backfill_outcome, 64, "backfill_outcome"),
                    "attempted_at": utc_now().isoformat(),
                }
            if _json_bytes(raw_metadata) > MAX_RAW_METADATA_BYTES:
                raise ValueError(f"raw_metadata exceeds {MAX_RAW_METADATA_BYTES} bytes")
            record.raw_metadata = raw_metadata
            if safe_metadata.get("imap_uid") is not None:
                record.imap_uid = _bounded_text(
                    safe_metadata["imap_uid"], 64, "imap_uid", allow_none=True
                )
            if safe_metadata.get("uid_validity") is not None:
                record.uid_validity = _bounded_text(
                    safe_metadata["uid_validity"], 128, "uid_validity", allow_none=True
                )
            if processing_status is not None:
                record.processing_status = _coalesce_status(processing_status, None)
                record.processing_error = _bounded_text(
                    processing_error,
                    MAX_PROCESSING_ERROR_LENGTH,
                    "processing_error",
                    allow_none=True,
                )
                record.processed_at = utc_now()
            record.updated_at = utc_now()
            session.flush()
            return record

    def update_processing_status(
        self,
        record_id: str,
        processing_status: str | RecruitmentMailProcessingStatus,
        *,
        processing_error: str | None | object = _UNSET,
    ) -> RecruitmentMailRecord:
        status_value = _coalesce_status(processing_status, None)
        assert status_value is not None
        error_value = (
            _bounded_text(
                processing_error,
                MAX_PROCESSING_ERROR_LENGTH,
                "processing_error",
                allow_none=True,
            )
            if processing_error is not _UNSET
            else _UNSET
        )
        self._initialize()
        with self.storage.write_transaction() as session:
            record = self._record_in_session(session, record_id=record_id)
            if record is None:
                raise KeyError("recruitment mail record was not found")
            record.processing_status = status_value
            if error_value is not _UNSET:
                record.processing_error = error_value
            record.processed_at = (
                utc_now()
                if status_value
                in {
                    RecruitmentMailProcessingStatus.PROCESSED.value,
                    RecruitmentMailProcessingStatus.LINKED.value,
                    RecruitmentMailProcessingStatus.IGNORED.value,
                    RecruitmentMailProcessingStatus.PROCESSED_UPDATED.value,
                    RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED.value,
                    RecruitmentMailProcessingStatus.IRRELEVANT.value,
                    RecruitmentMailProcessingStatus.PENDING_ASSOCIATION.value,
                    RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA.value,
                    RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value,
                    RecruitmentMailProcessingStatus.FAILED_TERMINAL.value,
                }
                else None
            )
            record.updated_at = utc_now()
            session.flush()
            return record

    def _record_in_session(
        self,
        session: Any,
        *,
        record_id: str | None = None,
        message_id: str | None = None,
        mailbox: str | None = None,
        account_ref: str | None = None,
        dedupe_key: str | None = None,
    ) -> RecruitmentMailRecord | None:
        if message_id is not None:
            if mailbox is None:
                if record_id is None:
                    raise TypeError("mailbox is required when looking up a message_id")
                mailbox, record_id = record_id, None
            return self._find_message_record(
                session,
                mailbox=mailbox,
                message_id=message_id,
                account_ref=account_ref,
            )
        elif dedupe_key is not None:
            statement = select(RecruitmentMailRecord).where(
                RecruitmentMailRecord.dedupe_key == _bounded_text(
                    dedupe_key, 128, "dedupe_key"
                )
            )
        elif record_id is not None:
            statement = select(RecruitmentMailRecord).where(
                RecruitmentMailRecord.id == _bounded_text(record_id, 128, "record_id")
            )
        else:
            raise TypeError("one mail lookup key is required")
        return session.scalar(statement)


RecruitmentEmailStore = RecruitmentMailStore


__all__ = [
    "MAX_BODY_LENGTH",
    "MAX_PARSED_RESULT_BYTES",
    "MAX_RAW_METADATA_BYTES",
    "RecruitmentEmailRecord",
    "RecruitmentEmailStore",
    "RecruitmentMail",
    "RecruitmentMailCursorRecord",
    "RecruitmentMailProcessingStatus",
    "RecruitmentMailRecord",
    "RecruitmentMailStore",
    "RecruitmentMailSyncItemRecord",
    "RecruitmentMailSyncRunRecord",
]
