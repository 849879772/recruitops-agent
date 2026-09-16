"""Prepare redacted, non-semantic mail input for later model analysis."""

from __future__ import annotations

import json
from hashlib import sha256

from . import sanitization
from .models import EmailMessage, ParsedRecruitmentEmail, RecruitmentMessageCategory


def _sanitised_content(
    message: EmailMessage,
) -> tuple[str, str, str, str | None, list[str], bool]:
    html_source = message.html_body if message.html_body is not None else message.body_text
    clean_body, _html_links, active_removed = sanitization._sanitise_html(html_source)
    safe_body = sanitization.redact_sensitive_text(clean_body)
    safe_subject = sanitization.redact_sensitive_text(message.subject)
    safe_sender = (
        sanitization.redact_sensitive_text(message.sender)
        if message.sender is not None
        else None
    )
    safe_recipients = [sanitization.redact_sensitive_text(item) for item in message.recipients]
    return clean_body, safe_body, safe_subject, safe_sender, safe_recipients, active_removed


def _message_content_digest(message: EmailMessage) -> str:
    """Mirror storage's digest basis without invoking semantic parsing."""

    _clean_body, safe_body, safe_subject, safe_sender, safe_recipients, _active_removed = (
        _sanitised_content(message)
    )
    digest_payload = {
        "sender": safe_sender,
        "recipients": safe_recipients,
        "subject": safe_subject,
        "body_text": safe_body,
    }
    return sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def prepare_mail_for_model(message: EmailMessage) -> ParsedRecruitmentEmail:
    """Sanitize one mail while deliberately leaving semantic analysis to a model."""

    clean_body, safe_body, safe_subject, safe_sender, safe_recipients, active_removed = (
        _sanitised_content(message)
    )
    source_text = "\n".join(item for item in (message.subject, clean_body) if item)
    safety_flags = sanitization._prompt_flags(source_text)
    if active_removed:
        safety_flags.append("active_html_content_removed")
    safety_flags = sanitization._dedupe_reasons(safety_flags)

    identity = sanitization._redacted_identity(message.identity)
    redacted_fields: list[str] = []
    if message.sender is not None and safe_sender != message.sender:
        redacted_fields.append("sender")
    if any(left != right for left, right in zip(message.recipients, safe_recipients)):
        redacted_fields.append("recipients")
    if safe_subject != message.subject:
        redacted_fields.append("subject")
    if safe_body != clean_body:
        redacted_fields.append("body_text")
    if (
        message.identity.account_ref is not None
        and message.identity.account_ref != identity.account_ref
    ):
        redacted_fields.append("identity.account_ref")

    return ParsedRecruitmentEmail(
        identity=identity,
        sender=safe_sender,
        recipients=safe_recipients,
        subject=safe_subject,
        body_text=safe_body,
        received_at=message.received_at,
        category=RecruitmentMessageCategory.OTHER,
        confidence=0.0,
        pending_confirmation_reasons=["model_analysis_pending"],
        safety_flags=safety_flags,
        redacted_fields=redacted_fields,
        requires_confirmation=False,
        category_evidence=[],
    )


__all__ = ["prepare_mail_for_model"]
