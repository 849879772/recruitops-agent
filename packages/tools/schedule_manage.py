"""Create and update local schedule items in the Agent snapshot store."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import logging
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.config import get_settings
from packages.domain.models import ScheduleEvent
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, ScheduleEventSnapshot

from .typed import (
    EvidenceSource,
    ToolErrorCode,
    ToolInput,
    ToolModel,
    ToolResponse,
    ToolStatus,
)


EventStatus = Literal["pending", "completed", "ignored"]
TimeKind = Literal["appointment", "deadline", "unspecified"]

logger = logging.getLogger(__name__)

_PATCH_FIELDS = frozenset(
    {
        "title",
        "event_type",
        "company_name",
        "job_title",
        "event_date",
        "event_time",
        "time_kind",
        "status",
        "application_id",
        "note",
        "location_or_link",
    }
)
_NON_NULLABLE_PATCH_FIELDS = frozenset(
    {"title", "event_type", "company_name", "job_title"}
)
def _safe_location_or_link(value: str | None) -> str | None:
    """Keep plain locations usable while rejecting unsafe URL-like values."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if any(ord(character) < 32 for character in value):
        raise ValueError("location_or_link must not contain control characters")

    parsed = urlsplit(value)
    looks_like_url = bool(parsed.scheme) or value.startswith("//") or "://" in value
    if not looks_like_url:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("location_or_link links must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("location_or_link links must not contain credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("location_or_link contains an invalid port") from exc
    if any(character.isspace() for character in value):
        raise ValueError("location_or_link links must not contain whitespace")
    return value


def _validate_date_time(event_date: date | None, event_time: time | None) -> None:
    if event_date is None and event_time is not None:
        raise ValueError("event_time requires event_date")


class ScheduleEventCreateFields(ToolModel):
    """Mutable fields accepted by the local UI create endpoint."""

    title: str = Field(min_length=1, max_length=512)
    event_type: str = Field(min_length=1, max_length=64)
    company_name: str = Field(min_length=1, max_length=255)
    job_title: str = Field(default="", max_length=512)
    event_date: date | None = None
    event_time: time | None = None
    time_kind: TimeKind = "appointment"
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=4000)
    location_or_link: str | None = Field(default=None, max_length=2048)

    @field_validator("note", "location_or_link")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("location_or_link")
    @classmethod
    def validate_location_or_link(cls, value: str | None) -> str | None:
        return _safe_location_or_link(value)

    @model_validator(mode="after")
    def validate_date_and_time(self) -> "ScheduleEventCreateFields":
        _validate_date_time(self.event_date, self.event_time)
        return self


class ScheduleEventPatchFields(ToolModel):
    """Partial mutable fields shared by the API and Agent update paths."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
    event_type: str | None = Field(default=None, min_length=1, max_length=64)
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=512)
    event_date: date | None = None
    event_time: time | None = None
    time_kind: TimeKind | None = None
    status: EventStatus | None = None
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=4000)
    location_or_link: str | None = Field(default=None, max_length=2048)

    @field_validator("note", "location_or_link")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("location_or_link")
    @classmethod
    def validate_location_or_link(cls, value: str | None) -> str | None:
        return _safe_location_or_link(value)

    @model_validator(mode="after")
    def validate_patch_fields(self) -> "ScheduleEventPatchFields":
        supplied = self.model_fields_set & _PATCH_FIELDS
        if not supplied:
            raise ValueError("at least one schedule field is required")
        if any(
            field_name in self.model_fields_set and getattr(self, field_name) is None
            for field_name in _NON_NULLABLE_PATCH_FIELDS
        ):
            raise ValueError("title, event_type, company_name, and job_title cannot be null")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "time_kind" in self.model_fields_set and self.time_kind is None:
            raise ValueError("time_kind cannot be null")
        return self


class ScheduleManageInput(ScheduleEventPatchFields):
    """Flat MCP input for one idempotent create or one event update.

    Create requires ``request_key``, ``title``, ``event_type`` and
    ``company_name``; ``job_title`` may be empty. Update requires ``event_id``
    and at least one mutable schedule field.
    """

    timeout_ms: int = Field(default=5_000, ge=1, le=120_000)
    action: Literal["create", "update"]
    request_key: str | None = Field(default=None, min_length=1, max_length=255)
    event_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "ScheduleManageInput":
        if self.action == "create":
            required = ("title", "event_type", "company_name")
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ValueError(f"create requires: {', '.join(missing)}")
            if not self.request_key:
                raise ValueError("request_key is required for create")
            if "status" in self.model_fields_set:
                raise ValueError("status is only supported for update")
            if "time_kind" in self.model_fields_set and self.time_kind is None:
                raise ValueError("time_kind cannot be null")
            if self.event_id is not None:
                raise ValueError("event_id is only supported for update")
            _validate_date_time(self.event_date, self.event_time)
            return self
        if not self.event_id:
            raise ValueError("event_id is required for update")
        return self


class ScheduleManageData(ToolModel):
    event: ScheduleEvent
    created: bool = False
    updated: bool = False


class ScheduleManageResponse(ToolResponse[ScheduleManageData]):
    read_only: Literal[False] = False


class ScheduleManageError(ValueError):
    """Base error that can be projected to an API or typed-tool response."""


class ScheduleNotFoundError(ScheduleManageError):
    pass


class ScheduleConflictError(ScheduleManageError):
    pass


class ScheduleValidationError(ScheduleManageError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_time_kind(
    event_date: date | None,
    time_kind: TimeKind | str | None,
) -> TimeKind:
    """Keep the date/time meaning explicit without inventing a duration."""

    if event_date is None:
        return "unspecified"
    if time_kind == "unspecified":
        return "appointment"
    return time_kind if time_kind in {"appointment", "deadline"} else "appointment"


def _same_binding_text(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _event_fields(fields: ScheduleEventCreateFields | ScheduleManageInput) -> dict[str, Any]:
    return {
        "title": fields.title,
        "event_type": fields.event_type,
        "company_name": fields.company_name,
        "job_title": fields.job_title or "",
        "event_date": fields.event_date,
        "event_time": fields.event_time,
        "time_kind": _canonical_time_kind(
            fields.event_date,
            getattr(fields, "time_kind", None) or "appointment",
        ),
        "application_id": fields.application_id,
        "note": fields.note,
        "location_or_link": fields.location_or_link,
    }


def _stored_event_fields(row: ScheduleEventSnapshot) -> dict[str, Any]:
    return {
        "title": row.title,
        "event_type": row.event_type,
        "company_name": row.company_name,
        "job_title": row.job_title or "",
        "event_date": row.event_date,
        "event_time": row.event_time,
        "time_kind": _canonical_time_kind(
            row.event_date,
            getattr(row, "time_kind", None) or "appointment",
        ),
        "application_id": row.application_id,
        "note": _optional_text(row.note),
        "location_or_link": _optional_text(row.location_or_link),
    }


def _event_from_row(row: ScheduleEventSnapshot) -> ScheduleEvent:
    return ScheduleEvent(
        id=row.id,
        title=row.title,
        event_date=row.event_date,
        status=getattr(row, "status", None) or "pending",
        time_kind=_canonical_time_kind(
            row.event_date,
            getattr(row, "time_kind", None) or "appointment",
        ),
        event_time=row.event_time,
        event_type=row.event_type,
        company_name=row.company_name,
        job_title=row.job_title,
        application_stage=row.application_stage,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        application_id=row.application_id,
        location_or_link=row.location_or_link,
        note=row.note,
        created_at=row.created_at,
        updated_at=row.updated_at,
        source=row.source,
        source_ref=row.source_ref,
    )


def _validate_source(source: str, source_ref: str) -> tuple[str, str]:
    normalized_source = source.strip() if isinstance(source, str) else ""
    normalized_ref = source_ref.strip() if isinstance(source_ref, str) else ""
    if not normalized_source or not normalized_ref:
        raise ScheduleValidationError("source and source_ref are required")
    if len(normalized_source) > 128 or len(normalized_ref) > 512:
        raise ScheduleValidationError("source or source_ref is too long")
    return normalized_source, normalized_ref


class ScheduleManager:
    """Shared storage service used by local UI and MCP write boundaries."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def create(
        self,
        fields: ScheduleEventCreateFields | ScheduleManageInput,
        *,
        source: str,
        source_ref: str,
    ) -> ScheduleManageData:
        source, source_ref = _validate_source(source, source_ref)
        values = _event_fields(fields)
        _validate_date_time(values["event_date"], values["event_time"])

        try:
            with self.storage.write_transaction() as session:
                existing = session.scalar(
                    select(ScheduleEventSnapshot).where(
                        ScheduleEventSnapshot.source == source,
                        ScheduleEventSnapshot.source_ref == source_ref,
                    )
                )
                application = self._application(session, values["application_id"])
                if application is not None:
                    values["company_name"], values["job_title"] = self._bound_values(
                        application,
                        company_name=values["company_name"],
                        job_title=values["job_title"],
                    )
                if existing is not None:
                    self._assert_same_request(existing, values)
                    return ScheduleManageData(event=_event_from_row(existing), created=False)

                event = ScheduleEventSnapshot(
                    id="schedule-" + uuid4().hex,
                    source=source,
                    source_ref=source_ref,
                    title=values["title"],
                    event_date=values["event_date"],
                    status="pending",
                    time_kind=values["time_kind"],
                    event_time=values["event_time"],
                    event_type=values["event_type"],
                    company_name=values["company_name"],
                    job_title=values["job_title"],
                    application_stage=application.stage if application is not None else "applied",
                    starts_at=None,
                    ends_at=None,
                    application_id=application.id if application is not None else None,
                    location_or_link=values["location_or_link"],
                    note=values["note"],
                )
                session.add(event)
                session.flush()
                return ScheduleManageData(event=_event_from_row(event), created=True)
        except IntegrityError:
            # The unique source pair is the idempotency guard under concurrent creates.
            existing = self._existing(source, source_ref)
            if existing is None:
                raise
            self._assert_same_request(existing, values)
            return ScheduleManageData(event=_event_from_row(existing), created=False)

    def update(
        self,
        event_id: str,
        fields: ScheduleEventPatchFields,
        *,
        expected_updated_at: datetime | None = None,
    ) -> ScheduleManageData:
        normalized_id = event_id.strip() if isinstance(event_id, str) else ""
        if not normalized_id:
            raise ScheduleValidationError("event_id is required")

        with self.storage.write_transaction() as session:
            event = session.scalar(
                select(ScheduleEventSnapshot)
                .where(ScheduleEventSnapshot.id == normalized_id)
                .with_for_update()
            )
            if event is None:
                raise ScheduleNotFoundError(f"schedule event {normalized_id!r} was not found")
            if expected_updated_at is not None and _utc(event.updated_at) != _utc(expected_updated_at):
                raise ScheduleConflictError("schedule event changed; refresh before updating")

            supplied = fields.model_fields_set & _PATCH_FIELDS
            next_application_id = (
                fields.application_id
                if "application_id" in supplied
                else event.application_id
            )
            next_company_name = (
                fields.company_name
                if "company_name" in supplied
                else event.company_name
            )
            next_job_title = (
                fields.job_title
                if "job_title" in supplied
                else event.job_title
            ) or ""
            application = self._application(session, next_application_id)
            if application is not None:
                next_company_name, next_job_title = self._bound_values(
                    application,
                    company_name=next_company_name,
                    job_title=next_job_title,
                )

            next_date = fields.event_date if "event_date" in supplied else event.event_date
            next_time = fields.event_time if "event_time" in supplied else event.event_time
            next_time_kind = (
                fields.time_kind
                if "time_kind" in supplied
                else getattr(event, "time_kind", None) or "appointment"
            )
            next_time_kind = _canonical_time_kind(next_date, next_time_kind)
            _validate_date_time(next_date, next_time)

            if "application_id" in supplied:
                event.application_id = application.id if application is not None else None
                event.application_stage = application.stage if application is not None else "applied"
            if "company_name" in supplied or (
                application is not None and event.company_name != next_company_name
            ):
                event.company_name = next_company_name
            if "job_title" in supplied or (
                application is not None and event.job_title != next_job_title
            ):
                event.job_title = next_job_title
            for field_name in supplied - {
                "application_id",
                "company_name",
                "job_title",
                "event_date",
                "event_time",
                "time_kind",
            }:
                setattr(event, field_name, getattr(fields, field_name))
            if "event_date" in supplied:
                event.event_date = next_date
            if "event_time" in supplied:
                event.event_time = next_time
            if event.time_kind != next_time_kind or "time_kind" in supplied:
                event.time_kind = next_time_kind

            if event.time_kind == "deadline" or supplied & {"event_date", "event_time", "time_kind"}:
                # No duration is accepted by this boundary, so it cannot safely
                # preserve or synthesize appointment bounds after a time edit.
                event.starts_at = None
                event.ends_at = None
            event.updated_at = _now()
            session.flush()
            return ScheduleManageData(event=_event_from_row(event), updated=True)

    @staticmethod
    def _assert_same_request(row: ScheduleEventSnapshot, values: dict[str, Any]) -> None:
        if _stored_event_fields(row) != values:
            raise ScheduleConflictError(
                "request_key is already bound to a different schedule payload"
            )

    @staticmethod
    def _bound_values(
        application: ApplicationSnapshot,
        *,
        company_name: str,
        job_title: str,
    ) -> tuple[str, str]:
        if not _same_binding_text(company_name, application.company_name):
            raise ScheduleValidationError(
                f"company_name does not match application {application.id!r}; "
                "reselect the application"
            )
        if job_title and not _same_binding_text(job_title, application.job_title):
            raise ScheduleValidationError(
                f"job_title does not match application {application.id!r}; "
                "reselect the application"
            )
        # An explicitly selected application is the canonical source for its
        # company/job identity; an empty job is completed from that record.
        return application.company_name, application.job_title or ""

    @staticmethod
    def _application(session: Any, application_id: str | None) -> ApplicationSnapshot | None:
        if application_id is None:
            return None
        application = session.get(ApplicationSnapshot, application_id)
        if application is None:
            raise ScheduleNotFoundError(f"application {application_id!r} was not found")
        return application

    def _existing(self, source: str, source_ref: str) -> ScheduleEventSnapshot | None:
        with self.storage.session() as session:
            return session.scalar(
                select(ScheduleEventSnapshot).where(
                    ScheduleEventSnapshot.source == source,
                    ScheduleEventSnapshot.source_ref == source_ref,
                )
            )


def _response(
    request: ScheduleManageInput,
    *,
    status: ToolStatus,
    data: ScheduleManageData | None,
    error_code: ToolErrorCode | None = None,
    error_message: str | None = None,
    started: float,
) -> ScheduleManageResponse:
    return ScheduleManageResponse(
        tool_name="schedule_manage",
        status=status,
        success=status == ToolStatus.SUCCESS,
        data=data,
        evidence=[
            EvidenceSource(
                source="schedule_event_snapshots",
                source_ref=data.event.id if data is not None else None,
            )
        ],
        error_code=error_code,
        error_message=error_message,
        timeout_ms=request.timeout_ms,
        timed_out=False,
        elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
        read_only=False,
    )


def schedule_manage(
    request: ScheduleManageInput,
    storage: Storage | None,
    *,
    write_enabled: bool | None = None,
) -> ScheduleManageResponse:
    """Execute one local schedule mutation without the application approval flow."""

    started = perf_counter()
    if storage is None:
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message="Agent storage is not configured for schedule_manage.",
            started=started,
        )
    enabled = (
        bool(getattr(get_settings(), "write_enabled", False))
        if write_enabled is None
        else bool(write_enabled)
    )
    if not enabled:
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            error_code=ToolErrorCode.READ_ONLY_VIOLATION,
            error_message="Schedule writes are disabled.",
            started=started,
        )

    manager = ScheduleManager(storage)
    try:
        if request.action == "create":
            data = manager.create(
                request,
                source="agent_schedule",
                source_ref=request.request_key or "",
            )
        else:
            data = manager.update(
                request.event_id or "",
                request,
                expected_updated_at=request.expected_updated_at,
            )
    except ScheduleNotFoundError as exc:
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            error_code=ToolErrorCode.NOT_FOUND,
            error_message=str(exc),
            started=started,
        )
    except (ScheduleConflictError, ScheduleValidationError) as exc:
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            error_code=ToolErrorCode.INVALID_INPUT,
            error_message=str(exc),
            started=started,
        )
    except Exception as exc:
        # Keep SQL/database details out of both the model-visible response and
        # diagnostics; exception text and tracebacks may contain bound values.
        logger.error(
            "schedule_manage storage operation failed (%s)",
            type(exc).__name__,
        )
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message="Schedule storage was unavailable.",
            started=started,
        )
    return _response(request, status=ToolStatus.SUCCESS, data=data, started=started)


__all__ = [
    "EventStatus",
    "ScheduleEventCreateFields",
    "ScheduleEventPatchFields",
    "ScheduleManageData",
    "ScheduleManageError",
    "ScheduleManageInput",
    "ScheduleManageResponse",
    "ScheduleManager",
    "ScheduleConflictError",
    "ScheduleNotFoundError",
    "ScheduleValidationError",
    "TimeKind",
    "schedule_manage",
]
