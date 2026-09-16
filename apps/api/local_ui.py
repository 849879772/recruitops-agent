"""Same-origin loopback UI operations; no user-entered API token."""
from contextvars import ContextVar
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from packages.config import get_settings
from packages.domain.models import ApplicationStage
from packages.storage import Storage, ApplicationSnapshot
from packages.storage import JobSnapshot, CompanySnapshot
from hashlib import sha256
from packages.storage.models import ScheduleEventSnapshot
from packages.recruitment_mail.storage import RecruitmentMailRecord

local_ui_request = ContextVar("local_ui_request", default=False)
router = APIRouter(prefix="/api/local-ui/applications", tags=["applications"])


def is_local_ui(request: Request) -> bool:
    if request.headers.get("x-recruitops-local-ui") != "1":
        return False
    origin = urlsplit(request.headers.get("origin", ""))
    return (request.url.hostname in {"localhost", "127.0.0.1", "::1"}
            and origin.scheme == request.url.scheme
            and origin.netloc == request.url.netloc
            and request.headers.get("sec-fetch-site", "same-origin") == "same-origin")


def _storage():
    if not local_ui_request.get():
        raise HTTPException(403, "Local same-origin UI request required")
    settings = get_settings()
    if not settings.write_enabled:
        raise HTTPException(403, "Writes are disabled")
    return Storage.from_url(settings.database_url)


class ApplicationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: ApplicationStage
    result: Literal["待", "进行中", "通过", "淘汰", "放弃"] = "进行中"
    note: str = Field(default="", max_length=4000)
    expected_updated_at: datetime


class ApplicationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=255)


@router.post("")
def record_application(body: ApplicationRecord):
    with _storage().write_transaction() as session:
        job = session.get(JobSnapshot, body.job_id)
        if job is None:
            raise HTTPException(404, "岗位不存在")
        company = session.get(CompanySnapshot, job.company_id)
        company_name = company.name if company else job.company_id
        identity = sha256((company_name + "\n" + job.title).encode()).hexdigest()
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": int(identity[:15], 16)})
        from sqlalchemy import or_, and_
        rows = list(session.scalars(select(ApplicationSnapshot).where(or_(
            ApplicationSnapshot.job_id == job.id,
            and_(ApplicationSnapshot.company_name == company_name, ApplicationSnapshot.job_title == job.title),
        ))))
        if len(rows) > 1:
            raise HTTPException(409, "存在多条相符投递，请到投递记录页核对")
        created = not rows
        if rows:
            row = rows[0]
            if not row.job_id:
                row.job_id = job.id
        else:
            row = ApplicationSnapshot(id="manual-" + identity[:24], company_name=company_name,
                job_title=job.title, job_id=job.id, stage="applied", record_url=None,
                idempotency_key="manual:" + identity, source="manual", source_ref=identity,
                stage_history=[{"stage": "applied", "result": "进行中", "source": "manual",
                                "note": "用户点击记录投递", "at": datetime.now(timezone.utc).isoformat()}])
            session.add(row)
        session.flush()
        return {"application_id": row.id, "created": created, "stage": row.stage}


def _row(session, application_id, expected):
    row = session.scalar(select(ApplicationSnapshot).where(
        ApplicationSnapshot.id == application_id).with_for_update())
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.updated_at.replace(tzinfo=timezone.utc) != expected.astimezone(timezone.utc):
        raise HTTPException(409, "记录已变化，请刷新后重试")
    return row


@router.patch("/{application_id}")
def edit_application(application_id: str, body: ApplicationEdit):
    with _storage().write_transaction() as session:
        row = _row(session, application_id, body.expected_updated_at)
        now = datetime.now(timezone.utc)
        row.stage_history = [*(row.stage_history or []), {
            "stage": body.stage.value, "previous_stage": row.stage,
            "result": body.result, "note": body.note,
            "source": "manual", "at": now.isoformat(),
        }]
        row.stage = ("rejected" if body.result == "淘汰" else
                     "withdrawn" if body.result == "放弃" else body.stage.value)
        row.note = body.note
        row.source_stage = body.stage.value
        row.source_status = body.result
        row.source_status_synced_at = None
        row.updated_at = now
    return {"status": "updated"}


class ApplicationDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_updated_at: datetime


class ApplicationLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_url: str = Field(max_length=2048)
    expected_updated_at: datetime

    @field_validator("record_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("请输入有效的 HTTP/HTTPS 投递进度页地址")
        if parsed.fragment.startswith("/job/"):
            raise ValueError("这是岗位详情页，请填写官网的我的投递/投递记录页面地址")
        return value


@router.patch("/{application_id}/record-url")
def bind_application_link(application_id: str, body: ApplicationLink):
    with _storage().write_transaction() as session:
        row = _row(session, application_id, body.expected_updated_at)
        row.record_url = body.record_url
        row.updated_at = datetime.now(timezone.utc)
    return {"status": "updated"}


@router.delete("/{application_id}")
def delete_application(application_id: str, body: ApplicationDelete):
    with _storage().write_transaction() as session:
        row = _row(session, application_id, body.expected_updated_at)
        events = session.scalars(select(ScheduleEventSnapshot).where(
            ScheduleEventSnapshot.application_id == application_id)).all()
        mails = session.scalars(select(RecruitmentMailRecord).where(
            RecruitmentMailRecord.application_id == application_id)).all()
        snapshot = lambda item: {c.name: getattr(item, c.name) for c in item.__table__.columns}
        backup = Path(".data/backups") / f"manual-application-delete-{uuid4().hex}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({"application": snapshot(row),
            "events": [snapshot(e) for e in events],
            "mail_links": [{"id": m.id, "application_id": m.application_id} for m in mails]},
            ensure_ascii=False, default=str), encoding="utf-8")
        for mail in mails:
            mail.application_id = None
        for event in events:
            session.delete(event)
        session.delete(row)
    return {"status": "deleted"}


class ApplicationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str = Field(min_length=1, max_length=64)
    event_date: date
    event_time: time | None = None
    note: str = Field(default="", max_length=4000)


@router.post("/{application_id}/events")
def add_event(application_id: str, body: ApplicationEvent):
    with _storage().write_transaction() as session:
        row = session.get(ApplicationSnapshot, application_id)
        if row is None:
            raise HTTPException(404, "Application not found")
        key = uuid4().hex
        session.add(ScheduleEventSnapshot(id=key, source="manual", source_ref=key,
            application_id=row.id, company_name=row.company_name, job_title=row.job_title,
            application_stage=row.stage, title=f"{row.company_name} · {body.event_type}",
            event_type=body.event_type, event_date=body.event_date,
            event_time=body.event_time, note=body.note))
    return {"status": "created"}
