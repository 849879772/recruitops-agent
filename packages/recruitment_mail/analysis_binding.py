"""Bind a model proposal to source text before using the existing guarded writer."""

from .model_analysis import MailAnalysisProposal, validate_mail_analysis_proposal
from .models import CompanyCandidate, JobCandidate, ParsedRecruitmentEmail, RecruitmentMessageCategory
import re
from .record_view import parsed_record

from .identity import normalize_company_name, normalize_job_title, mail_matches_application, company_names_match


def parsed_model_evidence(record, payload: dict) -> ParsedRecruitmentEmail:
    proposal = MailAnalysisProposal.model_validate(payload)
    validate_mail_analysis_proposal(proposal, record)
    if not proposal.evidence_quotes:
        raise ValueError("missing_event_evidence")
    source = f"{record.subject}\n{record.body_text}\n{record.sender or ''}"
    if proposal.company_name and normalize_company_name(proposal.company_name) not in normalize_company_name(source):
        raise ValueError("company_not_in_source")
    if proposal.job_title and normalize_job_title(proposal.job_title) not in normalize_job_title(source):
        raise ValueError("job_not_in_source")
    if proposal.job_code and proposal.job_code.casefold() not in source.casefold():
        raise ValueError("job_code_not_in_source")
    original = parsed_record(record)
    category = (
        proposal.event_type.value if proposal.event_type.value in {x.value for x in RecruitmentMessageCategory}
        else "other"
    )
    quote = proposal.evidence_quotes[0][:1000]
    return original.model_copy(update={
        "subject": record.subject, "body_text": record.body_text,
        "received_at": record.received_at,
        "category": RecruitmentMessageCategory(category),
        "company_candidates": [CompanyCandidate(value=proposal.company_name, evidence=quote)] if proposal.company_name else [],
        "job_candidates": [JobCandidate(value=proposal.job_title, evidence=quote)] if proposal.job_title else [],
        "category_evidence": proposal.evidence_quotes,
        "time_candidates": [], "deadline_candidates": [],
        "location_candidates": [], "link_candidates": [],
        "pending_confirmation_reasons": [], "requires_confirmation": False,
    })


def model_application_matches(record, payload, application):
    parsed = parsed_model_evidence(record, payload)
    from .binding import confirmed_binding_matches
    confirmed = confirmed_binding_matches(record, application)
    if confirmed is not None:
        return confirmed
    if mail_matches_application(parsed, application):
        return True
    proposal = MailAnalysisProposal.model_validate(payload)
    if proposal.job_title or not proposal.job_code or not proposal.company_name:
        return False
    return company_names_match(proposal.company_name, application.company_name) and bool(
        re.search(r"(?<![A-Za-z0-9])" + re.escape(proposal.job_code) + r"(?![A-Za-z0-9])",
                  application.job_title, flags=re.IGNORECASE)
    )
