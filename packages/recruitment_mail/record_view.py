"""Read persisted mail even when its derived analysis cache was reset."""

from .models import MailIdentity, ParsedRecruitmentEmail, RecruitmentMessageCategory


def parsed_record(record) -> ParsedRecruitmentEmail:
    if record.parsed_result:
        return ParsedRecruitmentEmail.model_validate(record.parsed_result)
    # Resetting analysis must not discard transport identity or safety metadata.
    return ParsedRecruitmentEmail(
        identity=MailIdentity(message_id=record.message_id, mailbox=record.mailbox,
                              thread_id=record.thread_id, account_ref=record.account_ref),
        sender=record.sender, recipients=record.recipients,
        subject=record.subject, body_text=record.body_text, received_at=record.received_at,
        category=RecruitmentMessageCategory.OTHER, confidence=0,
        safety_flags=record.safety_flags, redacted_fields=record.redacted_fields,
        pending_confirmation_reasons=record.pending_confirmation_reasons,
        requires_confirmation=record.requires_confirmation,
    )
