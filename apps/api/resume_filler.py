"""Authenticated local resume-extension application registration."""
from hashlib import sha256
from secrets import compare_digest
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func
from packages.config import get_settings
from packages.storage import Storage, ApplicationSnapshot, JobSnapshot

router = APIRouter(prefix="/api/integrations/resume-filler")


class Registration(BaseModel):
    application_id: str | None = Field(default=None, max_length=255)
    company: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=512)
    record_url: str = Field(max_length=2048)
    city: str = Field(default="", max_length=255)

    @field_validator("company", "title")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Company and title are required")
        return value

    @field_validator("record_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("A recruitment page URL is required")
        if parsed.fragment.startswith("/job/"):
            raise ValueError("这是岗位详情页，请填写官网的我的投递/投递记录页面地址")
        return value


def authorized_settings(authorization):
    settings = get_settings()
    if not settings.api_token or not compare_digest(authorization or "", "Bearer " + settings.api_token):
        raise HTTPException(401, "Local API token is required")
    return settings


@router.get("/applications")
def applications(authorization: str | None = Header(default=None)):
    settings = authorized_settings(authorization)
    with Storage.from_url(settings.database_url).session() as session:
        rows = session.execute(select(ApplicationSnapshot, JobSnapshot.detail_url)
            .outerjoin(JobSnapshot, ApplicationSnapshot.job_id == JobSnapshot.id)
            .order_by(ApplicationSnapshot.updated_at.desc())).all()
        return {"items": [{"id": row.id, "company": row.company_name, "title": row.job_title,
            "record_url": row.record_url, "detail_url": detail_url, "stage": row.stage}
            for row, detail_url in rows]}

@router.post("/application")
def register(body: Registration, authorization: str | None = Header(default=None)):
    settings = authorized_settings(authorization)
    if not settings.write_enabled:
        raise HTTPException(403, "Writes are disabled")
    identity = sha256((body.company + "\n" + body.title).encode()).hexdigest()
    storage = Storage.from_url(settings.database_url)
    with storage.write_transaction() as session:
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": int(identity[:15], 16)})
        query = select(ApplicationSnapshot).with_for_update()
        if body.application_id:
            query = query.where(ApplicationSnapshot.id == body.application_id)
        else:
            query = query.where(ApplicationSnapshot.company_name == body.company, ApplicationSnapshot.job_title == body.title)
        matches = list(session.scalars(query))
        if body.application_id and not matches:
            raise HTTPException(404, "Application not found")
        if len(matches) > 1:
            raise HTTPException(409, "Multiple applications match; choose the record in Agent")
        row = matches[0] if matches else None
        if row and body.application_id and (row.company_name != body.company or row.job_title != body.title):
            raise HTTPException(409, "Selected application identity changed; refresh and select again")
        created = row is None
        if created:
            now = datetime.now(timezone.utc).isoformat()
            row = ApplicationSnapshot(id="resume-" + identity[:24], company_name=body.company,
                job_title=body.title, record_url=body.record_url, stage="applied",
                idempotency_key="resume-filler:" + identity, source="resume_filler", source_ref=identity,
                note=body.city, stage_history=[{"stage": "applied", "source": "resume_filler", "at": now}])
            session.add(row)
            session.flush()
        elif body.application_id or not row.record_url:
            row.record_url = body.record_url
            row.updated_at = datetime.now(timezone.utc)
        return {"ok": True, "application_id": row.id, "created": created, "current_stage": row.stage,
                "total": session.scalar(select(func.count()).select_from(ApplicationSnapshot))}
