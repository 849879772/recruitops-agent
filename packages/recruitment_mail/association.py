from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import re

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.models import Application, ApplicationStage, ScheduleEvent

from .models import ParsedRecruitmentEmail, RecruitmentMessageCategory


class AssociationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MailAuditEvidence(AssociationModel):
    source: str
    source_ref: str
    summary: str


class ApplicationMatch(AssociationModel):
    application_id: str
    company_name: str
    job_title: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class ApplicationStageDraft(AssociationModel):
    application_id: str
    current_stage: ApplicationStage
    target_stage: ApplicationStage
    source_status: str
    evidence: str
    evidence_refs: list[MailAuditEvidence] = Field(default_factory=list)


class ScheduleDraft(AssociationModel):
    application_id: str
    event_type: str
    starts_at: datetime
    location_or_link: str | None = None
    evidence: str
    evidence_refs: list[MailAuditEvidence] = Field(default_factory=list)


class RecruitmentMailAssociation(AssociationModel):
    status: str
    requires_confirmation: bool = False
    match: ApplicationMatch | None = None
    candidates: list[ApplicationMatch] = Field(default_factory=list)
    stage_draft: ApplicationStageDraft | None = None
    schedule_drafts: list[ScheduleDraft] = Field(default_factory=list)
    schedule_conflicts: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    evidence: list[MailAuditEvidence] = Field(default_factory=list)


_STAGE_BY_CATEGORY = {
    RecruitmentMessageCategory.APPLICATION_CONFIRMATION: ApplicationStage.APPLIED,
    RecruitmentMessageCategory.ASSESSMENT: ApplicationStage.APPLIED,
    RecruitmentMessageCategory.WRITTEN_TEST: ApplicationStage.WRITTEN,
    RecruitmentMessageCategory.INTERVIEW: ApplicationStage.INTERVIEW1,
    RecruitmentMessageCategory.OFFER: ApplicationStage.OFFER,
    RecruitmentMessageCategory.REJECTION: ApplicationStage.REJECTED,
}
_STAGE_ORDER = {
    ApplicationStage.INTERESTED: 0,
    ApplicationStage.APPLIED: 1,
    ApplicationStage.ASSESSMENT: 2,
    ApplicationStage.WRITTEN: 3,
    ApplicationStage.INTERVIEW1: 4,
    ApplicationStage.INTERVIEW2: 5,
    ApplicationStage.INTERVIEW3: 6,
    ApplicationStage.HR: 7,
    ApplicationStage.OFFER: 8,
    ApplicationStage.REJECTED: 9,
    ApplicationStage.WITHDRAWN: 9,
}


def _key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _company_key_matches(candidate: str, company: str) -> bool:
    """Accept common short names when their meaningful characters are preserved."""
    if not candidate or not company:
        return False
    if candidate in company or company in candidate:
        return True
    legal_suffixes = ("有限责任公司", "股份有限公司", "有限公司", "集团")
    compact_company = company
    for suffix in legal_suffixes:
        compact_company = compact_company.removesuffix(suffix)
    candidate_chars = set(candidate)
    meaningful = {char for char in compact_company if char not in {"省", "市", "区"}}
    return len(candidate) >= 3 and candidate_chars <= set(company) and len(candidate_chars & meaningful) >= 2


def _candidate_score(message: ParsedRecruitmentEmail, application: Application) -> ApplicationMatch:
    company = _key(application.company_name)
    title = _key(application.job_title)
    score = 0.0
    reasons: list[str] = []
    for item in message.company_candidates:
        candidate = _key(item.value)
        if candidate and company and _company_key_matches(candidate, company):
            score += 0.45 * item.confidence
            reasons.append("company_candidate" if candidate in company or company in candidate else "company_short_name")
            break
    for item in message.job_candidates:
        candidate = _key(item.value)
        if not candidate or not title:
            continue
        if candidate == title:
            score += 0.45 * item.confidence
            reasons.append("job_title_exact")
            break
        if candidate in title or title in candidate:
            score += 0.30 * item.confidence
            reasons.append("job_title_partial")
            break
    combined = _key(f"{message.subject}\n{message.body_text}")
    if company and company in combined:
        score += 0.12
        reasons.append("company_in_message")
    if title and title in combined:
        score += 0.18
        reasons.append("job_title_in_message")
    return ApplicationMatch(
        application_id=application.id,
        company_name=application.company_name,
        job_title=application.job_title,
        confidence=min(score, 1.0),
        reasons=reasons,
    )


def _job_core_key(value: str) -> str:
    """Normalize a job title without discarding direction, department, or ATS codes."""

    # Parenthesized text is part of many real ATS titles, for example a
    # department, direction, or position code.  `_key` removes punctuation but
    # deliberately retains the characters inside those wrappers.
    return _key(value)


def is_strong_application_match(
    message: ParsedRecruitmentEmail,
    application: Application,
) -> bool:
    """Recognize an unambiguous mail/application pair without semantic retrieval."""

    from .identity import mail_matches_application

    return mail_matches_application(message, application)


def _stage_draft(
    message: ParsedRecruitmentEmail,
    application: Application,
    evidence_refs: list[MailAuditEvidence],
) -> ApplicationStageDraft | None:
    target = _STAGE_BY_CATEGORY.get(message.category)
    if target is None or target is application.stage:
        return None
    if target is not ApplicationStage.REJECTED and _STAGE_ORDER[target] < _STAGE_ORDER[application.stage]:
        return None
    return ApplicationStageDraft(
        application_id=application.id,
        current_stage=application.stage,
        target_stage=target,
        source_status=message.category.value,
        evidence=f"mail:{message.identity.mailbox}/{message.identity.message_id}",
        evidence_refs=evidence_refs,
    )


def _schedule_drafts(
    message: ParsedRecruitmentEmail,
    application: Application,
    evidence_refs: list[MailAuditEvidence],
) -> list[ScheduleDraft]:
    event_type = {
        RecruitmentMessageCategory.ASSESSMENT: "测评",
        RecruitmentMessageCategory.WRITTEN_TEST: "笔试",
        RecruitmentMessageCategory.INTERVIEW: "面试",
        RecruitmentMessageCategory.OFFER: "Offer",
    }.get(message.category)
    if event_type is None:
        return []
    links = [item.url for item in message.link_candidates]
    locations = [item.value for item in message.location_candidates]
    deadline_values = {item.value for item in message.deadline_candidates}
    drafts: list[ScheduleDraft] = []
    for item in message.time_candidates:
        if item.ambiguous or item.value in deadline_values or not isinstance(item.normalized, datetime):
            continue
        drafts.append(
            ScheduleDraft(
                application_id=application.id,
                event_type=event_type,
                starts_at=item.normalized,
                location_or_link=locations[0] if locations else (links[0] if links else None),
                evidence=item.evidence,
                evidence_refs=evidence_refs,
            )
        )
    return drafts


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def find_stale_company_only_match(
    message: ParsedRecruitmentEmail,
    applications: Sequence[Application],
) -> Application | None:
    """Resolve an old backwards event when one company application can only remain unchanged."""

    from .identity import mail_company_matches_application

    target = _STAGE_BY_CATEGORY.get(message.category)
    if target is None or target is ApplicationStage.REJECTED or message.received_at is None:
        return None
    company_matches = [
        application
        for application in applications
        if mail_company_matches_application(message, application)
    ]
    if len(company_matches) != 1:
        return None
    application = company_matches[0]
    current_time = application.source_status_synced_at
    if current_time is None or _STAGE_ORDER[target] >= _STAGE_ORDER[application.stage]:
        return None
    return application if _utc(message.received_at) <= _utc(current_time) else None


def _event_bounds(event: ScheduleEvent) -> tuple[datetime, datetime] | None:
    if event.status != "pending" or event.time_kind != "appointment" or event.ends_at is None:
        return None
    start = event.starts_at
    if start is None and event.event_date is not None and event.event_time is not None:
        start = datetime.combine(event.event_date, event.event_time)
    if start is None:
        return None
    start = _utc(start)
    end = _utc(event.ends_at)
    if end <= start:
        return None
    return start, end


def _schedule_conflict_ids(
    drafts: Sequence[ScheduleDraft],
    schedule_events: Sequence[ScheduleEvent],
) -> list[str]:
    conflicts: list[str] = []
    for draft in drafts:
        start = _utc(draft.starts_at)
        for raw_event in schedule_events:
            event = (
                raw_event
                if isinstance(raw_event, ScheduleEvent)
                else ScheduleEvent.model_validate(raw_event)
            )
            bounds = _event_bounds(event)
            if bounds is None or not bounds[0] <= start < bounds[1]:
                continue
            event_id = str(event.id)
            if event_id not in conflicts:
                conflicts.append(event_id)
    return conflicts


def associate_recruitment_email(
    message: ParsedRecruitmentEmail,
    applications: list[Application],
    *,
    minimum_confidence: float = 0.65,
    minimum_margin: float = 0.15,
    schedule_events: Sequence[ScheduleEvent] = (),
    record_id: str | None = None,
) -> RecruitmentMailAssociation:
    source_ref = record_id or f"{message.identity.mailbox}/{message.identity.message_id}"
    evidence = [
        MailAuditEvidence(
            source="recruitment_mail",
            source_ref=source_ref,
            summary=f"Parsed recruitment email category: {message.category.value}",
        )
    ]
    if message.category is RecruitmentMessageCategory.OTHER:
        return RecruitmentMailAssociation(
            status="unresolved",
            requires_confirmation=True,
            review_reasons=["not_a_supported_recruitment_message"],
            evidence=evidence,
        )
    ranked = sorted(
        (_candidate_score(message, application) for application in applications),
        key=lambda item: (-item.confidence, item.application_id),
    )
    strong_applications = [
        application
        for application in applications
        if is_strong_application_match(message, application)
    ]
    if len(strong_applications) == 1:
        strong_application = strong_applications[0]
        strong_match = _candidate_score(message, strong_application).model_copy(
            update={
                "confidence": 1.0,
                "reasons": list(dict.fromkeys([
                    *_candidate_score(message, strong_application).reasons,
                    "strong_exact_application_match",
                ])),
            }
        )
        ranked = [
            strong_match,
            *[item for item in ranked if item.application_id != strong_application.id],
        ]
    elif not strong_applications:
        stale_application = find_stale_company_only_match(message, applications)
        if stale_application is not None:
            stale_match = _candidate_score(message, stale_application).model_copy(
                update={
                    "confidence": 1.0,
                    "reasons": ["stale_company_only_noop"],
                }
            )
            ranked = [
                stale_match,
                *[item for item in ranked if item.application_id != stale_application.id],
            ]
    plausible = [item for item in ranked if item.confidence > 0][:10]
    if not ranked or ranked[0].confidence < minimum_confidence:
        return RecruitmentMailAssociation(
            status="unresolved",
            requires_confirmation=True,
            candidates=plausible,
            review_reasons=["application_match_confidence_too_low"],
            evidence=evidence,
        )
    if len(ranked) > 1 and ranked[0].confidence - ranked[1].confidence < minimum_margin:
        return RecruitmentMailAssociation(
            status="ambiguous",
            requires_confirmation=True,
            candidates=plausible,
            review_reasons=["multiple_application_matches"],
            evidence=evidence,
        )
    match = ranked[0]
    application = next(item for item in applications if item.id == match.application_id)
    strong_match = is_strong_application_match(message, application)
    if strong_match:
        match = match.model_copy(
            update={
                "confidence": 1.0,
                "reasons": list(dict.fromkeys([*match.reasons, "strong_exact_application_match"])),
            }
        )
    reasons = list(message.pending_confirmation_reasons)
    if message.requires_confirmation:
        reasons.append("mail_evidence_requires_confirmation")
    stage_draft = _stage_draft(message, application, evidence)
    schedule_drafts = _schedule_drafts(message, application, evidence)
    schedule_conflicts = _schedule_conflict_ids(schedule_drafts, schedule_events)
    if schedule_conflicts:
        reasons.append("schedule_conflict")
    return RecruitmentMailAssociation(
        status="review_required" if reasons else "matched",
        requires_confirmation=bool(reasons),
        match=match,
        candidates=[match],
        stage_draft=stage_draft,
        schedule_drafts=schedule_drafts,
        schedule_conflicts=schedule_conflicts,
        review_reasons=list(dict.fromkeys(reasons)),
        evidence=evidence,
    )


__all__ = [
    "ApplicationMatch",
    "ApplicationStageDraft",
    "MailAuditEvidence",
    "RecruitmentMailAssociation",
    "ScheduleDraft",
    "associate_recruitment_email",
    "is_strong_application_match",
    "find_stale_company_only_match",
]
