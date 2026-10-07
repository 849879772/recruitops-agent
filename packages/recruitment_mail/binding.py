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


def bound_application_ids(record):
    """Versioned multi-bindings retain a primary ID for legacy consumers."""
    binding = (record.raw_metadata or {}).get(BINDING_KEY) or {}
    ids = binding.get("application_ids")
    if not isinstance(ids, list):
        ids = [binding.get("application_id") or getattr(record, "application_id", None)]
    return list(dict.fromkeys(str(value) for value in ids if value))


def binding_scope(record, proposal=None):
    analysis = (record.raw_metadata or {}).get("model_analysis", {})
    if proposal is None:
        proposal = analysis.get("payload", {}) if analysis.get("digest") == record.content_digest else {}
    specific = bool(proposal.get("job_title") or proposal.get("job_code"))
    multiple = bool(not specific and proposal.get("company_name")
                    and proposal.get("event_type") in {"assessment", "written_test"})
    return {"selection_scope": "company_event" if multiple else "job_specific" if specific else "single",
            "allows_multiple": multiple,
            "scope_reason": "company_test_event" if multiple else "job_specific_evidence" if specific else "event_scope_unconfirmed"}


def _target_ids(application_id=None, application_ids=None):
    if application_ids is not None:
        if not isinstance(application_ids, list) or len(application_ids) > 50:
            raise ValueError("invalid_binding_targets")
        ids = list(dict.fromkeys(application_ids))
        if any(not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 255 for value in ids):
            raise ValueError("invalid_binding_targets")
        if application_id is not None and (not ids or application_id != ids[0]):
            raise ValueError("binding_targets_conflict")
        return ids
    return [application_id] if application_id else []


def _validate_group(record, applications, proposal=None):
    if len(applications) < 2:
        return
    if not binding_scope(record, proposal)["allows_multiple"]:
        raise ValueError("mail_event_requires_single_application")
    proposal = proposal if proposal is not None else (record.raw_metadata or {}).get("model_analysis", {}).get("payload", {})
    from .analysis_binding import parsed_model_evidence
    parsed_model_evidence(record, proposal)
    if not all(company_names_match(proposal["company_name"], app.company_name) for app in applications):
        raise ValueError("multi_binding_company_mismatch")
    # An omitted model title cannot turn an explicitly named target into a
    # company-wide event. Human selection confirms identity, not event scope.
    from .identity import normalize_job_title
    source = normalize_job_title(f"{record.subject}\n{record.body_text}")
    if any(normalize_job_title(app.job_title) in source for app in applications):
        raise ValueError("mail_event_has_specific_job_evidence")


def confirmed_binding_matches(record, application):
    """Return None without a confirmation; False also blocks fallback after revocation."""
    binding = (record.raw_metadata or {}).get(BINDING_KEY)
    if not isinstance(binding, dict):
        return None
    if binding.get("content_digest") != record.content_digest:
        return False
    ids = bound_application_ids(record)
    identities = binding.get("identity_digests", {})
    identity_digest = identities.get(application.id) if identities else binding.get("identity_digest")
    return bool(binding.get("state") == "bound"
                and binding.get("authority") == "human_approval"
                and binding.get("approval_key")
                and application.id in ids
                and record.application_id == ids[0]
                and identity_digest == _digest(application_identity(application)))


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


def binding_candidates(store, record_id, *, query="", limit=20, offset=0):
    record = store.get(record_id=record_id)
    if record is None:
        raise KeyError("mail_not_found")
    analysis = (record.raw_metadata or {}).get("model_analysis", {})
    proposal = analysis.get("payload", {}) if analysis.get("digest") == record.content_digest else {}
    query = str(query).strip().casefold()
    rows = []
    current_applications = []
    with store.storage.session() as session:
        candidates = session.execute(select(ApplicationSnapshot, JobSnapshot).outerjoin(
            JobSnapshot, JobSnapshot.id == ApplicationSnapshot.job_id)).all()
        for application, job in candidates:
            if confirmed_binding_matches(record, application) is True:
                current_applications.append({"application_id": application.id,
                    "company_name": application.company_name, "job_title": application.job_title,
                    "stage": application.stage})
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
            recommended = bool(company_match and (not proposal.get("job_title") or title_match))
            rows.append({"application_id": application.id, "company_name": application.company_name,
                         "job_title": application.job_title, "stage": application.stage,
                         "city": getattr(job, "city", None) if job else None,
                         "identity_digest": _digest(application_identity(application)),
                         "reason": reason, "rank": rank, "recommended": recommended})
    rows.sort(key=lambda row: (-row["rank"], row["application_id"]))
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 50))
    return {"record_id": record.id, "subject": record.subject,
            "received_at": record.received_at.isoformat() if record.received_at else None,
            "excerpt": str(record.body_text or "")[:1500], "content_digest": record.content_digest,
            "binding_revision": binding_revision(record), "current_application_id": record.application_id,
            "current_application_ids": bound_application_ids(record),
            "current_applications": current_applications,
            "recommended_application_ids": [row["application_id"] for row in rows if row["recommended"]],
            **binding_scope(record),
            "candidates": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "has_more": offset + limit < len(rows),
            "requires_user_confirmation": True}


def binding_preview(store, record_id, *, application_id=None, application_ids=None, action="bind", task_id=None,
                    expected_digest=None, expected_revision=None):
    """Pure preview. Only an approved execution can persist its exact payload."""
    if action not in {"bind", "unbind", "correct"}:
        raise ValueError("invalid_binding_action")
    ids = _target_ids(application_id, application_ids)
    if (action == "unbind") != (not ids):
        raise ValueError("binding_target_required")
    record = store.get(record_id=record_id)
    if record is None:
        raise KeyError("mail_not_found")
    revision = binding_revision(record)
    if expected_digest is not None and expected_digest != record.content_digest:
        raise ValueError("mail_changed")
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("binding_changed")
    applications = []
    if ids:
        with store.storage.session() as session:
            for identifier in ids:
                application = session.get(ApplicationSnapshot, identifier)
                if application is None:
                    raise KeyError("application_not_found")
                applications.append(application)
            _validate_group(record, applications)
            identities = [application_identity(app) for app in applications]
    else:
        identities = []
    application_id = ids[0] if ids else None
    identity = identities[0] if identities else None
    before = {"record_id": record.id, "subject": record.subject, "sender": record.sender,
              "application_id": record.application_id, "application_ids": bound_application_ids(record), "binding_revision": revision}
    after = {"record_id": record.id, "application_id": application_id,
             "company_name": identity["company_name"] if identity else None,
             "job_title": identity["job_title"] if identity else None,
             "application_ids": ids, "applications": [dict(application_id=item["id"], **{key: item[key] for key in ("company_name", "job_title")}) for item in identities],
             "binding_revision": revision + 1, "action": action,
             "scope": "only_this_mail_identity; event_and_application_checks_still_required"}
    payload = {"record_id": record.id, "content_digest": record.content_digest,
               "expected_revision": revision, "previous_application_id": record.application_id,
               "previous_application_ids": bound_application_ids(record), "application_ids": ids,
               "application_id": application_id, "identity_digest": _digest(identity) if identity else None,
               "identity_digests": {item["id"]: _digest(item) for item in identities},
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
                for identifier in _target_ids(payload.get("application_id"), payload.get("application_ids")):
                    application = session.get(ApplicationSnapshot, identifier)
                    if application is None or not confirmed_binding_matches(record, application):
                        raise ValueError("application_changed_after_preview")
                return WriteEffect(before=binding, after=binding)
            if binding_revision(record) != payload["expected_revision"] or record.application_id != payload["previous_application_id"]:
                raise ValueError("binding_changed_after_preview")
            action = payload["action"]
            if action not in {"bind", "correct", "unbind"}:
                raise ValueError("invalid_binding_action")
            application_id = payload["application_id"]
            ids = _target_ids(application_id, payload.get("application_ids"))
            if (action == "unbind") != (not ids):
                raise ValueError("invalid_binding_target")
            applications = [session.scalar(select(ApplicationSnapshot).where(
                ApplicationSnapshot.id == identifier).with_for_update()) for identifier in ids]
            if any(application is None for application in applications):
                raise ValueError("application_changed_after_preview")
            _validate_group(record, applications)
            expected_identities = payload.get("identity_digests") or {application_id: payload.get("identity_digest")}
            if any(_digest(application_identity(app)) != expected_identities.get(app.id) for app in applications):
                raise ValueError("application_changed_after_preview")
            application = applications[0] if applications else None
            before = {"application_id": record.application_id, "job_id": record.job_id,
                      "company_id": record.company_id, "binding": binding}
            now = datetime.now(timezone.utc)
            new_binding = {"version": 2, "revision": binding_revision(record) + 1,
                "state": "bound" if application else "unbound", "authority": "human_approval",
                "approval_key": key, "content_digest": record.content_digest,
                "application_id": application_id, "identity_digest": payload["identity_digest"],
                "application_ids": ids, "identity_digests": {app.id: _digest(application_identity(app)) for app in applications},
                "applications": [{"application_id": app.id, "company_name": app.company_name, "job_title": app.job_title, "stage": app.stage} for app in applications],
                "confirmed_at": now.isoformat(), "action": action}
            metadata = dict(record.raw_metadata or {})
            metadata[BINDING_KEY] = new_binding
            # Full before/after is also retained by the approval execution audit.
            metadata["binding_recent_history"] = [*metadata.get("binding_recent_history", [])[-19:], {
                "revision": new_binding["revision"], "action": action, "approval_key": key,
                "previous_application_id": record.application_id, "application_id": application_id,
                "previous_application_count": len(bound_application_ids(record)), "application_count": len(ids),
                "content_digest": record.content_digest, "at": now.isoformat()}]
            # The approval is a new identity evidence version. It grants one
            # later explicit processing pass, never starts processing itself.
            metadata.pop("model_processing", None)
            metadata.pop("application_processing_receipt", None)
            metadata["schedule_associations"] = {"application_ids": ids,
                "associated_jobs": new_binding["applications"], "content_digest": record.content_digest,
                "binding_revision": new_binding["revision"]}
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
                    item.company_name = application.company_name
                    item.job_title = "、".join(app.job_title for app in applications)[:512]
                else:
                    item.job_title = ""
                item.application_stage = application.stage if application else "applied"
                item.updated_at = now
            record.raw_metadata = dict(metadata)
            after = {"application_id": record.application_id, "job_id": record.job_id,
                     "company_id": record.company_id, "binding": new_binding, "application_ids": ids,
                     "schedule_ids": [item.id for item in schedules]}
            return WriteEffect(before=before, after=after,
                rollback_payload={"requires_new_approval": True, "record_id": record.id,
                                  "previous_application_id": before["application_id"]})
