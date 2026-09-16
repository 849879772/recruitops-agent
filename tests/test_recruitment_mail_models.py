from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from packages.recruitment_mail import (
    EmailMessage,
    MailCursor,
    MailIdentity,
    RecruitmentMessageCategory,
)


def test_mail_identity_and_cursor_accept_provider_neutral_aliases() -> None:
    identity = MailIdentity(id="message-1", thread_id="thread-1", folder="INBOX")
    cursor = MailCursor(folder="INBOX", cursor="opaque-2")

    assert identity.message_id == "message-1"
    assert identity.id == "message-1"
    assert identity.mailbox == "INBOX"
    assert cursor.token == "opaque-2"
    assert cursor.cursor == "opaque-2"


def test_mail_models_are_strict_and_message_can_hold_html_without_connecting() -> None:
    message = EmailMessage(
        identity=MailIdentity(message_id="message-1"),
        subject="Interview",
        html_body="<p>local fixture</p>",
        received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert message.identity.message_id == "message-1"
    assert message.html_body == "<p>local fixture</p>"
    assert RecruitmentMessageCategory.INTERVIEW.value == "interview"
    with pytest.raises(ValidationError):
        MailCursor(mailbox="INBOX", invented_field="not allowed")
