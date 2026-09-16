"""Persist source-grounded mail actions without changing application stages."""
from datetime import date, time, timedelta
from hashlib import sha256
import re

from sqlalchemy import select

from packages.storage.models import ScheduleEventSnapshot
from .storage import RecruitmentMailRecord


EVENT_LABELS = {"assessment": "完成测评", "written_test": "参加笔试",
                "interview": "参加面试", "action_required": "处理招聘事项"}
DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?"
    r"(?:[\sT]*(?:(?:周|星期)[一二三四五六日天]\s*)?"
    r"(\d{1,2})[:：](\d{2})(?::\d{2})?)?"
)


def _dates(value):
    found = []
    for match in DATE_RE.finditer(value or ""):
        year, month, day, hour, minute = match.groups()
        try:
            day_value = date(int(year), int(month), int(day))
            clock = None
            if hour is not None:
                if hour == "24" and minute == "00":
                    day_value += timedelta(days=1)
                    clock = time(0)
                else:
                    clock = time(int(hour), int(minute))
            found.append((day_value, clock))
        except ValueError:
            continue
    return found


def grounded_time(value, source):
    """Only accept one explicit, source-present date; never infer relative dates."""
    selected = set(_dates(value))
    if len(selected) != 1:
        return None, None
    day, clock = next(iter(selected))
    source_dates = _dates(source)
    if (day, clock) in source_dates or (clock is None and any(d == day for d, _ in source_dates)):
        return day, clock
    return None, None


def ensure_mail_schedule(store, record, proposal, owner, application=None):
    event = proposal.event_type.value
    if event not in EVENT_LABELS:
        return None
    # Semantic selection belongs to the model, date normalization stays deterministic.
    is_deadline = not proposal.event_time
    selected = proposal.deadline if is_deadline else proposal.event_time
    day, clock = grounded_time(selected, record.subject + "\n" + record.body_text)
    kind = ("deadline" if is_deadline else "appointment") if day else "unspecified"
    key = "mail-event-" + sha256(record.id.encode()).hexdigest()[:32]
    with store.storage.write_transaction() as session:
        current = session.scalar(select(RecruitmentMailRecord).where(
            RecruitmentMailRecord.id == record.id).with_for_update())
        attempt = (current.raw_metadata or {}).get("model_processing", {}) if current else {}
        if (current is None or current.content_digest != record.content_digest
                or attempt.get("owner") != owner or attempt.get("state") != "running"):
            raise ValueError("processing_claim_lost")
        existing = session.scalar(select(ScheduleEventSnapshot).where(
            ScheduleEventSnapshot.source == "recruitment_mail_schedule",
            ScheduleEventSnapshot.source_ref == record.id))
        if existing is not None:
            return {"id": existing.id, "created": False, "status": existing.status,
                    "time_kind": existing.time_kind, "event_date": str(existing.event_date) if existing.event_date else None}
        label = EVENT_LABELS[event]
        company = proposal.company_name or "招聘邮件"
        item = ScheduleEventSnapshot(
            id=key, source="recruitment_mail_schedule", source_ref=record.id,
            title=f"{company} · {label}"[:512], event_type=label,
            company_name=company, job_title=proposal.job_title or "",
            application_id=application.id if application else None,
            application_stage=application.stage.value if application else "applied",
            event_date=day, event_time=clock, time_kind=kind, status="pending",
            note="请查看原邮件中的具体要求。" if day else "时间待确认，请查看原邮件或手动补充；未推算相对日期。",
        )
        session.add(item)
        return {"id": key, "created": True, "status": "pending", "time_kind": kind,
                "event_date": day.isoformat() if day else None}
