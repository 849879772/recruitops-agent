from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .connectors import ImapConnectionConfig, ImapReadOnlyConnector, MailConnectorError
from .models import RecruitmentMailProcessingStatus
from .storage import RecruitmentMailStore
from .authentication import AUTHENTICATION_VERSION, has_aligned_authentication


@dataclass(frozen=True)
class MailAuthenticationBackfillResult:
    outcome: str
    attempted: bool
    authenticated: bool


def _has_aligned_authentication(metadata: dict[str, Any]) -> bool:
    return has_aligned_authentication(metadata)


def backfill_mail_authentication(
    settings: object,
    store: RecruitmentMailStore,
    record_id: str,
    *,
    connector_factory: Callable[[ImapConnectionConfig], Any] = ImapReadOnlyConnector,
    force: bool = False,
) -> MailAuthenticationBackfillResult:
    """Fetch trusted headers once for one persisted message; never changes an application."""

    record = store.get(record_id=record_id)
    if record is None:
        return MailAuthenticationBackfillResult("not_found", False, False)
    if record.source != "imap_readonly":
        return MailAuthenticationBackfillResult("source_not_imap_readonly", False, False)
    existing_transport = (record.raw_metadata or {}).get("transport", {})
    if isinstance(existing_transport, dict) and _has_aligned_authentication(existing_transport):
        return MailAuthenticationBackfillResult("already_authenticated", False, True)
    marker = (record.raw_metadata or {}).get("authentication_backfill", {})
    if not force and isinstance(marker, dict) and marker.get("outcome") and marker.get("version") == AUTHENTICATION_VERSION:
        return MailAuthenticationBackfillResult(str(marker["outcome"]), False, False)
    if not getattr(settings, "mail_enabled", False):
        return MailAuthenticationBackfillResult("mail_disabled", False, False)
    required = ("mail_imap_host", "mail_imap_username", "mail_imap_password")
    if any(not getattr(settings, name, None) for name in required):
        return MailAuthenticationBackfillResult("mail_configuration_missing", False, False)

    config = ImapConnectionConfig(
        host=getattr(settings, "mail_imap_host"),
        port=getattr(settings, "mail_imap_port", 993),
        username=getattr(settings, "mail_imap_username"),
        password=getattr(settings, "mail_imap_password"),
        mailbox=record.mailbox,
        timeout_seconds=min(30.0, max(1.0, float(getattr(settings, "mail_timeout_seconds", 30.0)))),
    )
    connector = connector_factory(config)
    try:
        if record.imap_uid:
            metadata = connector.fetch_source_metadata(
                record.imap_uid,
                expected_uid_validity=record.uid_validity,
            )
        else:
            if record.received_at is None:
                raise MailConnectorError("legacy_mail_has_no_received_at")
            metadata = connector.find_exact_source_metadata(
                subject=record.subject,
                received_at=record.received_at,
                body_text=record.body_text,
                limit=100,
            )
    except (MailConnectorError, OSError, ValueError) as exc:
        outcome = str(exc)[:64] or "metadata_fetch_failed"
        store.update_source_metadata(
            record.id,
            existing_transport if isinstance(existing_transport, dict) else {},
            processing_status=RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA,
            processing_error=f"{outcome}; retryable=false",
            backfill_outcome=outcome,
        )
        return MailAuthenticationBackfillResult(outcome, True, False)

    authenticated = _has_aligned_authentication(metadata)
    outcome = "authenticated" if authenticated else "no_aligned_dkim"
    store.update_source_metadata(
        record.id,
        metadata,
        processing_status=(
            RecruitmentMailProcessingStatus.PENDING
            if authenticated
            else RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA
        ),
        processing_error=None if authenticated else "no_aligned_dkim; retryable=false",
        backfill_outcome=outcome,
    )
    return MailAuthenticationBackfillResult(outcome, True, authenticated)


__all__ = ["MailAuthenticationBackfillResult", "backfill_mail_authentication"]
