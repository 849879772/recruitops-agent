"""Same-origin loopback UI operations; no user-entered API token."""
from contextvars import ContextVar
from datetime import date, datetime, time, timezone
import json
import os
from pathlib import Path
import re
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


def _desktop_completion_target(settings):
    instance_id = os.environ.get("RECRUITOPS_DESKTOP_INSTANCE_ID", "")
    run_id = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
    if (not local_ui_request.get() or os.environ.get("RECRUITOPS_ENV") != "desktop-isolated"
            or not re.fullmatch(r"[0-9a-f]{32}", instance_id)
            or not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or os.environ.get("RECRUITOPS_DESKTOP_WRITE_OPTIN") != instance_id
            or os.environ.get("RECRUITOPS_WRITE_ENABLED") != "true"
            or settings.write_enabled is not True):
        raise HTTPException(403, "当前独立实例尚未授权完成配置")
    try:
        raw_root = Path(os.environ.get("RECRUITOPS_AGENT_ROOT", ""))
        if not raw_root.is_absolute() or not raw_root.is_dir():
            raise ValueError()
        # Check only fixed ownership/config paths; never enumerate personal data.
        targets = [raw_root, *raw_root.parents, raw_root / "instance.json",
                   raw_root / "config", raw_root / "config/runtime-capabilities.json",
                   raw_root / ".data", raw_root / ".data/settings",
                   raw_root / ".data/settings/preferences.json",
                   raw_root / ".data/settings/candidate_profile.yaml"]
        for path in targets:
            if path.is_symlink():
                raise ValueError()
            if path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError()
        root = raw_root.resolve(strict=True)
        if Path(settings.agent_root).resolve(strict=True) != root:
            raise ValueError()
        record = json.loads((root / "instance.json").read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("schema") != 1
                or record.get("postgres_major") != 16 or record.get("instance_id") != instance_id
                or record.get("run_id") != run_id or record.get("root") != str(root)
                or record.get("state") != "ready"):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise HTTPException(403, "无法验证当前独立实例的配置目录") from None
    return root / "config/runtime-capabilities.json", instance_id


def validate_desktop_onboarding(settings, profile):
    """Validate prospective config before writes, without enabling any capability."""
    target = _desktop_completion_target(settings)
    matching = getattr(profile, "matching", None)
    facts = [*(getattr(profile, "skills", None) or []),
             *(getattr(matching, "project_evidence", None) or []),
             *(getattr(matching, "supporting_skills", None) or [])]
    present = lambda value: isinstance(value, str) and bool(value.strip())
    model_ready = all(present(getattr(settings, key, None)) for key in (
        "llm_api_key", "model_api_base_url", "model_name"))
    if (not model_ready or not any(present(fact) for fact in facts)
            or not any(present(word) for word in (getattr(matching, "title_keywords", None) or []))
            or not getattr(settings, "offerbiu_industry_groups", None)):
        raise HTTPException(422, "请先完成模型连接、简历事实、岗位关键词和行业范围配置")
    if (any(getattr(settings, key, False) for key in (
            "job_analysis_enabled", "codex_runtime_enabled", "vision_enabled"))
            and not settings.llm_enabled):
        raise HTTPException(422, "评分、助理或视觉能力需要先启用模型调用")
    if getattr(settings, "mail_enabled", False) and not all(
            present(getattr(settings, key, None)) for key in (
                "mail_imap_host", "mail_imap_username", "mail_imap_password")):
        raise HTTPException(422, "启用招聘邮箱前请完成 IMAP 连接配置")
    if getattr(settings, "mail_sync_on_startup", False) and not settings.mail_enabled:
        raise HTTPException(422, "启动时同步需要先启用招聘邮箱")
    return target


def complete_desktop_onboarding(settings, profile):
    """Called only after an explicit completion request and successful config save."""
    from packages.automation.latest_report import write_json_atomic

    target, instance_id = validate_desktop_onboarding(settings, profile)
    try:
        write_json_atomic(target, {"schema": 1, "instance_id": instance_id,
                                   "first_run_complete": True})
    except OSError:
        raise HTTPException(503, "配置已保存，但完成标记写入失败，请重试；当前能力未改变") from None
    return {"first_run_complete": True, "restart_required": True}


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
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, min_length=1, max_length=512)
    expected_updated_at: datetime

    @field_validator("company_name", "job_title")
    @classmethod
    def nonblank_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("公司和岗位名称不能为空")
        return value


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
    # PostgreSQL preserves offsets; SQLite returns stored UTC without tzinfo.
    actual = row.updated_at
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    if actual.astimezone(timezone.utc) != expected.astimezone(timezone.utc):
        raise HTTPException(409, "记录已变化，请刷新后重试")
    return row


@router.patch("/{application_id}")
def edit_application(application_id: str, body: ApplicationEdit):
    with _storage().write_transaction() as session:
        row = _row(session, application_id, body.expected_updated_at)
        now = datetime.now(timezone.utc)
        company_name = body.company_name if body.company_name is not None else row.company_name
        job_title = body.job_title if body.job_title is not None else row.job_title
        identity_changed = (company_name, job_title) != (row.company_name, row.job_title)
        if identity_changed:
            identity = sha256((company_name + "\n" + job_title).encode()).hexdigest()
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": int(identity[:15], 16)})
            existing = session.scalar(select(ApplicationSnapshot.id).where(
                ApplicationSnapshot.id != application_id,
                ApplicationSnapshot.company_name == company_name,
                ApplicationSnapshot.job_title == job_title,
            ))
            if existing is not None:
                raise HTTPException(409, "已有相同公司和岗位的投递记录，请先核对")
            events = session.scalars(select(ScheduleEventSnapshot).where(
                ScheduleEventSnapshot.application_id == application_id)).all()
            for event in events:
                if event.title == f"{row.company_name} · {event.event_type}":
                    event.title = f"{company_name} · {event.event_type}"
                if event.company_name == row.company_name:
                    event.company_name = company_name
                if event.job_title == row.job_title:
                    event.job_title = job_title
        history = {
            "stage": body.stage.value, "previous_stage": row.stage,
            "result": body.result, "note": body.note,
            "source": "manual", "at": now.isoformat(),
        }
        if identity_changed:
            history.update(previous_company_name=row.company_name,
                           previous_job_title=row.job_title,
                           company_name=company_name, job_title=job_title)
        row.stage_history = [*(row.stage_history or []), history]
        row.company_name = company_name
        row.job_title = job_title
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


class ManualApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company_name: str = Field(min_length=1, max_length=255)
    job_title: str = Field(min_length=1, max_length=512)
    stage: ApplicationStage = ApplicationStage.APPLIED
    record_url: str | None = Field(default=None, max_length=2048)
    note: str = Field(default="", max_length=4000)

    @field_validator("record_url")
    @classmethod
    def validate_record_url(cls, value):
        return ApplicationLink.valid_url(value) if value else None


@router.post("/manual")
def create_manual_application(body: ManualApplicationCreate):
    identity = sha256((body.company_name + "\n" + body.job_title).encode()).hexdigest()
    with _storage().write_transaction() as session:
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": int(identity[:15], 16)})
        rows = list(session.scalars(select(ApplicationSnapshot).where(
            ApplicationSnapshot.company_name == body.company_name,
            ApplicationSnapshot.job_title == body.job_title,
        )))
        if len(rows) > 1:
            raise HTTPException(409, "存在多条相符投递，请先核对已有记录")
        if rows:
            return {"application_id": rows[0].id, "created": False, "stage": rows[0].stage}
        result = "淘汰" if body.stage == ApplicationStage.REJECTED else (
            "放弃" if body.stage == ApplicationStage.WITHDRAWN else "进行中")
        row = ApplicationSnapshot(
            id="manual-" + identity[:24], company_name=body.company_name,
            job_title=body.job_title, stage=body.stage.value, record_url=body.record_url,
            note=body.note, source="manual", source_ref=identity,
            idempotency_key="manual:" + identity,
            stage_history=[{"stage": body.stage.value, "result": result, "source": "manual",
                            "note": body.note, "at": datetime.now(timezone.utc).isoformat()}],
        )
        session.add(row)
        session.flush()
        return {"application_id": row.id, "created": True, "stage": row.stage}


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
