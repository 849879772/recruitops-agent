"""Durable discovery-source records and bounded attempt history."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.storage import Storage
from packages.storage.models import Base, utc_now


SourceStatus = Literal["pending", "running", "complete", "partial", "failed", "unusable"]
SOURCE_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "complete", "partial", "failed", "unusable"}
)
_SENSITIVE_URL_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|auth|authorization|cookie|"
    r"signature|sig|access[_-]?token|refresh[_-]?token|oauth)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/?#@]+@")


class CompanySourceRecord(Base):
    """One source-side company/entry, independent of job snapshot ingestion."""

    __tablename__ = "company_source_records"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_record_id", name="uq_company_source_records_source_record"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'partial', 'failed', 'unusable')",
            name="ck_company_source_records_status",
        ),
        Index("ix_company_source_records_status", "status"),
        Index("ix_company_source_records_updated_at", "updated_at"),
        Index("ix_company_source_records_company_id", "company_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(512), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    entry_url: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    original_entry_url: Mapped[str] = mapped_column(
        String(2048), default="", nullable=False
    )
    final_url: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    failure_stage: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    jd_pending_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_success_job_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    pagination_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now,
        server_default=func.now(), nullable=False
    )


class CompanySourceAttempt(Base):
    """Immutable receipt for one source fetch attempt."""

    __tablename__ = "company_source_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'partial', 'failed', 'unusable')",
            name="ck_company_source_attempts_status",
        ),
        Index("ix_company_source_attempts_record_attempted", "record_id", "attempted_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("company_source_records.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempted_url: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    final_url: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    failure_stage: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    jd_pending_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    pagination_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )


class CompanySourceNotFound(LookupError):
    """Raised when a source record ID is not present."""


class CompanySourceConflict(ValueError):
    """Raised when an optimistic concurrency check fails."""


def _stable_record_id(source: str, source_record_id: str) -> str:
    return hashlib.sha256(f"{source}\0{source_record_id}".encode("utf-8")).hexdigest()


def _required_text(value: object, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _optional_text(value: object, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _bounded_attempt_reason(detail: str) -> str:
    """Keep the input boundary, but never abort a batch for verbose diagnostics."""
    marker = "\n[truncated: reason exceeds 10000 characters]"
    return detail if len(detail) <= 10000 else detail[:10000 - len(marker)] + marker


def _attempt_reason(reason_code: object, reason: object, status: str) -> tuple[str, str]:
    """Separate legacy exception codes from bounded, explicitly truncated details."""
    code = str(reason_code or "").strip()
    detail = str(reason or "").strip()
    failed = status in {"failed", "unusable"}
    if (not code or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", code)
            or (not failed and len(code) <= 128)):
        return code, _bounded_attempt_reason(detail)
    prefix = code.partition(":")[0]
    normalized = (
        prefix if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", prefix)
        else "source_attempt_failed" if failed else "source_attempt_note"
    )
    if code not in detail:
        detail = f"{detail}\n{code}" if detail else code
    return normalized, _bounded_attempt_reason(detail)


def _redact_url_text(value: object, field: str) -> str:
    """Keep URL evidence without retaining credentials or secret query values."""

    text = _optional_text(value, field, 2048)
    if not text:
        return text
    try:
        parsed = urlsplit(text)
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if any(_SENSITIVE_URL_KEY_RE.search(key) for key, _ in pairs):
                pairs = [
                    (key, "[REDACTED]" if _SENSITIVE_URL_KEY_RE.search(key) else value)
                    for key, value in pairs
                ]
                text = urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), parsed.fragment)
                )
                parsed = urlsplit(text)
        if parsed.username is not None or parsed.password is not None:
            hostname = parsed.hostname or ""
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            try:
                port = parsed.port
            except ValueError:
                port = None
            authority = f"[REDACTED]@{hostname}"
            if port is not None:
                authority += f":{port}"
            text = urlunsplit(
                (parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment)
            )
    except ValueError:
        text = _URL_USERINFO_RE.sub(r"\g<scheme>[REDACTED]@", text)
    return text


def validate_public_http_url(value: object, field: str = "entry_url") -> str:
    """Reject non-HTTP, credential-bearing, and obviously private URLs."""

    url = _required_text(value, field, 2048)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{field} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    hostname = hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal", ".lan")
    ):
        raise ValueError(f"{field} must not target a private host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError(f"{field} must not target a private host")
    return url


def _validate_status(status: str) -> str:
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unsupported source status: {status}")
    return status


def _validate_count(value: int, field: str) -> int:
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _attempt_dict(row: CompanySourceAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "record_id": row.record_id,
        "status": row.status,
        "attempted_url": row.attempted_url,
        "final_url": row.final_url,
        "failure_stage": row.failure_stage,
        "reason_code": row.reason_code,
        "reason": row.reason,
        "job_count": row.job_count,
        "jd_pending_count": row.jd_pending_count,
        "pagination_complete": row.pagination_complete,
        "attempted_at": _iso(row.attempted_at),
    }


def _record_dict(
    row: CompanySourceRecord,
    *,
    attempts: list[CompanySourceAttempt] | None = None,
    attempts_total: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row.id,
        "company_name": row.company_name,
        "source": row.source,
        "source_record_id": row.source_record_id,
        "source_url": row.source_url,
        "company_id": row.company_id,
        "entry_url": row.entry_url,
        "original_entry_url": row.original_entry_url,
        "final_url": row.final_url,
        "status": row.status,
        "failure_stage": row.failure_stage,
        "reason_code": row.reason_code,
        "reason": row.reason,
        "job_count": row.job_count,
        "jd_pending_count": row.jd_pending_count,
        "last_success_job_count": row.last_success_job_count,
        "pagination_complete": row.pagination_complete,
        "last_attempt_at": _iso(row.last_attempt_at),
        "updated_at": _iso(row.updated_at),
    }
    if attempts is not None:
        result["attempts"] = [_attempt_dict(attempt) for attempt in attempts]
        result["attempts_total"] = attempts_total if attempts_total is not None else len(attempts)
    return result


class CompanySourceRegistry:
    """Storage-backed source registry; it never requires a job snapshot."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def upsert_source(
        self,
        *,
        source: str,
        source_record_id: str,
        company_name: str,
        source_url: str,
        entry_url: str,
        company_id: str | None = None,
        status: SourceStatus = "pending",
    ) -> dict[str, Any]:
        source = _required_text(source, "source", 128)
        source_record_id = _required_text(source_record_id, "source_record_id", 512)
        company_name = _required_text(company_name, "company_name", 255)
        company_id = _optional_text(company_id, "company_id", 255) or None
        source_url = _redact_url_text(source_url, "source_url")
        entry_url = _redact_url_text(entry_url, "entry_url")
        status = _validate_status(status)
        unsafe_reasons: list[str] = []
        for field, value in (("source_url", source_url), ("entry_url", entry_url)):
            if not value:
                continue
            try:
                validate_public_http_url(value, field)
            except ValueError as exc:
                unsafe_reasons.append(str(exc))
        if unsafe_reasons:
            status = "unusable"
        record_id = _stable_record_id(source, source_record_id)
        with self.storage.write_transaction() as session:
            row = session.scalar(
                select(CompanySourceRecord)
                .where(
                    and_(
                        CompanySourceRecord.source == source,
                        CompanySourceRecord.source_record_id == source_record_id,
                    )
                )
                .with_for_update()
            )
            if row is None:
                row = CompanySourceRecord(
                    id=record_id,
                    source=source,
                    source_record_id=source_record_id,
                    company_name=company_name,
                    company_id=company_id,
                    source_url=source_url,
                    entry_url=entry_url,
                    original_entry_url=entry_url,
                    status=status,
                    failure_stage="ingest" if unsafe_reasons else "",
                    reason_code="unsafe_url" if unsafe_reasons else "",
                    reason="; ".join(unsafe_reasons),
                )
                session.add(row)
            else:
                row.company_name = company_name
                if company_id is not None:
                    row.company_id = company_id
                # Discovery refreshes must not overwrite the first URL or a
                # manually supplied entry; they may fill an empty value.
                if not row.source_url and source_url:
                    row.source_url = source_url
                if not row.entry_url and entry_url:
                    row.entry_url = entry_url
                    if not row.original_entry_url:
                        row.original_entry_url = entry_url
                manual_entry = bool(row.entry_url and row.entry_url != row.original_entry_url)
                if unsafe_reasons and not manual_entry and row.status in {
                    "pending", "failed", "unusable"
                }:
                    row.status = "unusable"
                    row.failure_stage = "ingest"
                    row.reason_code = "unsafe_url"
                    row.reason = "; ".join(unsafe_reasons)
                elif not unsafe_reasons and (status != "pending" or row.status == "pending"):
                    row.status = status
                row.updated_at = utc_now()
        return self.get_source(record_id)  # type: ignore[return-value]

    def record_attempt(
        self,
        record_id: str,
        *,
        status: SourceStatus,
        attempted_url: str = "",
        final_url: str = "",
        failure_stage: str = "",
        reason_code: str = "",
        reason: str = "",
        job_count: int = 0,
        jd_pending_count: int = 0,
        pagination_complete: bool | None = None,
    ) -> dict[str, Any]:
        status = _validate_status(status)
        attempted_url = _redact_url_text(attempted_url, "attempted_url")
        final_url = _redact_url_text(final_url, "final_url")
        failure_stage = _optional_text(failure_stage, "failure_stage", 128)
        reason_code, reason = _attempt_reason(reason_code, reason, status)
        job_count = _validate_count(job_count, "job_count")
        jd_pending_count = _validate_count(jd_pending_count, "jd_pending_count")
        now = utc_now()
        with self.storage.write_transaction() as session:
            row = session.get(CompanySourceRecord, record_id, with_for_update=True)
            if row is None:
                raise CompanySourceNotFound(record_id)
            attempt = CompanySourceAttempt(
                id=uuid4().hex,
                record_id=row.id,
                status=status,
                attempted_url=attempted_url,
                final_url=final_url,
                failure_stage=failure_stage,
                reason_code=reason_code,
                reason=reason,
                job_count=job_count,
                jd_pending_count=jd_pending_count,
                pagination_complete=pagination_complete,
                attempted_at=now,
            )
            session.add(attempt)
            row.status = status
            row.failure_stage = failure_stage
            row.reason_code = reason_code
            row.reason = reason
            if final_url:
                row.final_url = final_url
            if status in {"complete", "partial"}:
                row.job_count = job_count
                row.jd_pending_count = jd_pending_count
                row.last_success_job_count = job_count
                row.pagination_complete = pagination_complete
            elif pagination_complete is not None:
                row.pagination_complete = pagination_complete
            row.last_attempt_at = now
            row.updated_at = now
        return self.get_source(record_id)  # type: ignore[return-value]

    def set_entry_url(
        self, record_id: str, entry_url: str, expected_updated_at: datetime
    ) -> dict[str, Any]:
        entry_url = validate_public_http_url(entry_url)
        with self.storage.write_transaction() as session:
            row = session.get(CompanySourceRecord, record_id, with_for_update=True)
            if row is None:
                raise CompanySourceNotFound(record_id)
            if row.status == "running":
                raise CompanySourceConflict(record_id)
            if _utc(row.updated_at) != _utc(expected_updated_at):
                raise CompanySourceConflict(record_id)
            row.entry_url = entry_url
            if not row.original_entry_url:
                row.original_entry_url = entry_url
            row.updated_at = utc_now()
        return self.get_source(record_id)  # type: ignore[return-value]

    def list_sources(
        self, *, page: int = 1, page_size: int = 30, q: str = "",
        status: str | None = None, include_unusable: bool = False
    ) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("invalid pagination")
        if status is not None:
            _validate_status(status)
        q = _optional_text(q, "q", 255)
        filters = []
        if q:
            pattern = f"%{q}%"
            filters.append(
                or_(
                    CompanySourceRecord.company_name.ilike(pattern),
                    CompanySourceRecord.source.ilike(pattern),
                    CompanySourceRecord.source_record_id.ilike(pattern),
                )
            )
        if status is not None:
            filters.append(CompanySourceRecord.status == status)
        if not include_unusable:
            filters.append(CompanySourceRecord.status != "unusable")
        with self.storage.session() as session:
            statement = select(CompanySourceRecord).where(*filters)
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            rows = session.scalars(
                statement.order_by(
                    CompanySourceRecord.updated_at.desc(), CompanySourceRecord.id.desc()
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        return {
            "items": [_record_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_source(self, record_id: str) -> dict[str, Any] | None:
        with self.storage.session() as session:
            row = session.get(CompanySourceRecord, record_id)
            if row is None:
                return None
            attempts_total = session.scalar(
                select(func.count())
                .select_from(CompanySourceAttempt)
                .where(CompanySourceAttempt.record_id == record_id)
            ) or 0
            attempts = session.scalars(
                select(CompanySourceAttempt)
                .where(CompanySourceAttempt.record_id == record_id)
                .order_by(
                    CompanySourceAttempt.attempted_at.desc(), CompanySourceAttempt.id.desc()
                )
                .limit(50)
            ).all()
            return _record_dict(row, attempts=attempts, attempts_total=attempts_total)

    # Short aliases keep the registry convenient for pipeline callers.
    def list(self, **kwargs: Any) -> dict[str, Any]:
        return self.list_sources(**kwargs)

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self.get_source(record_id)


__all__ = [
    "CompanySourceAttempt",
    "CompanySourceConflict",
    "CompanySourceNotFound",
    "CompanySourceRecord",
    "CompanySourceRegistry",
    "SOURCE_STATUSES",
    "SourceStatus",
    "validate_public_http_url",
]
