"""Single-mail identity confirmation through the existing human approval center."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.approval.models import ApprovalPreview, EvidenceRef, OperationName
from packages.approval.executor import WriteEffect
from packages.storage import ApplicationSnapshot, JobSnapshot
from packages.storage.models import ScheduleEventSnapshot

from .identity import company_names_match, job_titles_match
from .storage import RecruitmentMailRecord


BINDING_KEY = "confirmed_application_binding"


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def application_identity(application):
    return {key: getattr(application, key, None) for key in (
        "id", "company_name", "job_title", "job_id", "record_url", "source", "source_ref")}


def binding_revision(record):
    return int((record.raw_metadata or {}).get(BINDING_KEY, {}).get("revision", 0))


def confirmed_binding_matches(record, application):
    """Return None without a confirmation; False also blocks fallback after revocation."""
    binding = (record.raw_metadata or {}).get(BINDING_KEY)
    if not isinstance(binding, dict):
        return None
    if binding.get("content_digest") != record.content_digest:
        return False
    return bool(binding.get("state") == "bound"
                and binding.get("authority") == "human_approval"
                and binding.get("approval_key")
                and binding.get("application_id") == application.id
                and record.application_id == application.id
                and binding.get("identity_digest") == _digest(application_identity(application)))


def _source_urls(record):
    return re.findall(r"https?://[^\s<>\"'）)]+", f"{record.subject}\n{record.body_text}")


def _structured_ats_match(record, proposal, application, job):
    """Rank explicit ATS IDs only inside company and observed platform/tenant scope."""
    if job is None or not job.source_platform or not job.source_tenant:
        return False
    if not company_names_match(proposal.get("company_name", ""), application.company_name):
        return False
    code = str(proposal.get("job_code") or "").strip()
    source = f"{record.subject}\n{record.body_text}"
    if not code or not re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", source, re.I):
        return False
    if code != str(job.native_job_id or ""):
        return False
    expected_host = urlsplit(job.detail_url).hostname
    tenant = str(job.source_tenant).casefold()
    return any(urlsplit(url).hostname == expected_host and tenant in
               {part.casefold() for part in re.split(r"[./?=&_-]+", urlsplit(url).netloc + urlsplit(url).path + "?" + urlsplit(url).query)}
               for url in _source_urls(record))


def binding_candidates(store, record_id, *, query="", limit=20):
    record = store.get(record_id=record_id)
    if record is None:
        raise KeyError("mail_not_found")
    analysis = (record.raw_metadata or {}).get("model_analysis", {})
    proposal = analysis.get("payload", {}) if analysis.get("digest") == record.content_digest else {}
    query = str(query).strip().casefold()
    rows = []
    with store.storage.session() as session:
        candidates = session.execute(select(ApplicationSnapshot, JobSnapshot).outerjoin(
            JobSnapshot, JobSnapshot.id == ApplicationSnapshot.job_id)).all()
        for application, job in candidates:
            if query and query not in f"{application.company_name} {application.job_title} {application.id}".casefold():
                continue
            company_match = company_names_match(proposal.get("company_name", ""), application.company_name)
            title_match = job_titles_match(proposal.get("job_title", ""), application.job_title)
            structured = _structured_ats_match(record, proposal, application, job)
            reason = "confirmed_binding" if confirmed_binding_matches(record, application) else (
                "ats_identity_candidate" if structured else "company_and_title" if company_match and title_match
                else "company_candidate" if company_match else "manual_selection")
            rank = {"confirmed_binding": 4, "ats_identity_candidate": 3, "company_and_title": 2,
                    "company_candidate": 1, "manual_selection": 0}[reason]
            # Unrelated records are available through explicit user search, never
            # presented as guessed automatic matches.
            if not query and rank == 0:
                continue
            rows.append({"application_id": application.id, "company_name": application.company_name,
                         "job_title": application.job_title, "stage": application.stage,
                         "identity_digest": _digest(application_identity(application)),
                         "reason": reason, "rank": rank})
    rows.sort(key=lambda row: (-row["rank"], row["application_id"]))
    return {"record_id": record.id, "content_digest": record.content_digest,
            "binding_revision": binding_revision(record), "current_application_id": record.application_id,
            "candidates": rows[:max(1, min(int(limit), 50))], "total": len(rows),
            "requires_user_confirmation": True}


def binding_preview(store, record_id, *, application_id=None, action="bind", task_id=None,
                    expected_digest=None, expected_revision=None):
    """Pure preview. Only an approved execution can persist its exact payload."""
    if action not in {"bind", "unbind", "correct"}:
        raise ValueError("invalid_binding_action")
    if (action == "unbind") != (application_id is None):
        raise ValueError("binding_target_required")
    record = store.get(record_id=record_id)
    if record is None:
        raise KeyError("mail_not_found")
    revision = binding_revision(record)
    if expected_digest is not None and expected_digest != record.content_digest:
        raise ValueError("mail_changed")
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("binding_changed")
    identity = None
    if application_id is not None:
        with store.storage.session() as session:
            application = session.get(ApplicationSnapshot, application_id)
            if application is None:
                raise KeyError("application_not_found")
            identity = application_identity(application)
    before = {"record_id": record.id, "subject": record.subject, "sender": record.sender,
              "application_id": record.application_id, "binding_revision": revision}
    after = {"record_id": record.id, "application_id": application_id,
             "company_name": identity["company_name"] if identity else None,
             "job_title": identity["job_title"] if identity else None,
             "binding_revision": revision + 1, "action": action,
             "scope": "only_this_mail_identity; event_and_sender_checks_still_required"}
    payload = {"record_id": record.id, "content_digest": record.content_digest,
               "expected_revision": revision, "previous_application_id": record.application_id,
               "application_id": application_id, "identity_digest": _digest(identity) if identity else None,
               "action": action}
    key = "mail-binding:" + _digest(payload)
    payload["approval_key"] = key
    now = datetime.now(timezone.utc)
    return ApprovalPreview(task_id=task_id or key, operation=OperationName.RECRUITMENT_MAIL_BINDING,
        idempotency_key=key, evidence_summary="Confirm only this mail association: " + _digest({"before": before, "after": after, "payload": payload}),
        evidence=(EvidenceRef(source="recruitment_mail", source_ref=record.id,
                              summary="User must confirm the displayed single-mail target"),),
        target_id=record.id, payload=payload, before=before, after=after,
        created_at=now, expires_at=now + timedelta(minutes=15))


class MailBindingAdapter:
    def __init__(self, storage):
        self.storage = storage

    def bind_recruitment_mail(self, payload):
        with self.storage.write_transaction() as session:
            record = session.scalar(select(RecruitmentMailRecord).where(
                RecruitmentMailRecord.id == payload["record_id"]).with_for_update())
            if record is None:
                raise KeyError("mail_not_found")
            binding = (record.raw_metadata or {}).get(BINDING_KEY, {})
            key = payload["approval_key"]
            if record.content_digest != payload["content_digest"]:
                raise ValueError("mail_changed_after_preview")
            if binding.get("approval_key") == key:
                application = session.get(ApplicationSnapshot, payload["application_id"]) if payload["application_id"] else None
                if payload["application_id"] and (application is None or not confirmed_binding_matches(record, application)):
                    raise ValueError("application_changed_after_preview")
                return WriteEffect(before=binding, after=binding)
            if binding_revision(record) != payload["expected_revision"] or record.application_id != payload["previous_application_id"]:
                raise ValueError("binding_changed_after_preview")
            action = payload["action"]
            if action not in {"bind", "correct", "unbind"}:
                raise ValueError("invalid_binding_action")
            application_id = payload["application_id"]
            if (action == "unbind") != (application_id is None):
                raise ValueError("invalid_binding_target")
            application = session.scalar(select(ApplicationSnapshot).where(
                ApplicationSnapshot.id == application_id).with_for_update()) if application_id else None
            identity = application_identity(application) if application is not None else None
            if application_id is not None and (identity is None or _digest(identity) != payload["identity_digest"]):
                raise ValueError("application_changed_after_preview")
            before = {"application_id": record.application_id, "job_id": record.job_id,
                      "company_id": record.company_id, "binding": binding}
            now = datetime.now(timezone.utc)
            new_binding = {"version": 1, "revision": binding_revision(record) + 1,
                "state": "bound" if application else "unbound", "authority": "human_approval",
                "approval_key": key, "content_digest": record.content_digest,
                "application_id": application_id, "identity_digest": payload["identity_digest"],
                "confirmed_at": now.isoformat(), "action": action}
            metadata = dict(record.raw_metadata or {})
            metadata[BINDING_KEY] = new_binding
            # Full before/after is also retained by the approval execution audit.
            metadata["binding_recent_history"] = [*metadata.get("binding_recent_history", [])[-19:], {
                "revision": new_binding["revision"], "action": action, "approval_key": key,
                "previous_application_id": record.application_id, "application_id": application_id,
                "content_digest": record.content_digest, "at": now.isoformat()}]
            # The approval is a new identity evidence version. It grants one
            # later explicit processing pass, never starts processing itself.
            metadata.pop("model_processing", None)
            record.raw_metadata = metadata
            record.application_id = application_id
            record.job_id = application.job_id if application else None
            job = session.get(JobSnapshot, application.job_id) if application and application.job_id else None
            record.company_id = job.company_id if job else None
            record.processing_status = "pending" if application else "pending_association"
            record.processing_error = None
            record.updated_at = now
            # Correct the association on this mail's existing item; never create
            # a second schedule or alter its event, status, notes, or time.
            schedules = session.scalars(select(ScheduleEventSnapshot).where(
                ScheduleEventSnapshot.source == "recruitment_mail_schedule",
                ScheduleEventSnapshot.source_ref == record.id).with_for_update()).all()
            for item in schedules:
                item.application_id = application_id
                if application:
                    item.company_name, item.job_title = application.company_name, application.job_title
                item.updated_at = now
            after = {"application_id": record.application_id, "job_id": record.job_id,
                     "company_id": record.company_id, "binding": new_binding,
                     "schedule_ids": [item.id for item in schedules]}
            return WriteEffect(before=before, after=after,
                rollback_payload={"requires_new_approval": True, "record_id": record.id,
                                  "previous_application_id": before["application_id"]})
