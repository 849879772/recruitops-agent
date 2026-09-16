from pathlib import Path

import pytest

from packages.config import Settings
from packages.recruitment_mail import (
    EmailMessage,
    MailCursor,
    MailFetchBatch,
    MailIdentity,
    MailRuntimeConfigurationError,
    RecruitmentMailStore,
    mail_account_key,
    sync_configured_mail,
)
from packages.storage import Storage


class FakeConnector:
    def fetch_since(self, cursor: MailCursor | None = None, *, limit: int = 100) -> MailFetchBatch:
        return MailFetchBatch(
            messages=[
                EmailMessage(
                    identity=MailIdentity(message_id="mail-1"),
                    subject="Offer notification",
                    body_text="Congratulations, your offer is ready.",
                )
            ],
            next_cursor=MailCursor(token="1"),
        )


def _store(tmp_path: Path) -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}"))


def test_disabled_mail_safely_stops_before_connector_creation(tmp_path: Path) -> None:
    created = []
    with pytest.raises(MailRuntimeConfigurationError, match="disabled"):
        sync_configured_mail(
            Settings(_env_file=None),
            _store(tmp_path),
            connector_factory=lambda config: created.append(config),
        )
    assert created == []


def test_configured_mail_sync_uses_hashed_account_key(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        mail_enabled=True,
        mail_imap_host="imap.example.com",
        mail_imap_username="candidate@example.com",
        mail_imap_password="secret-value",
    )
    seen = []
    store = _store(tmp_path)

    result = sync_configured_mail(
        settings,
        store,
        connector_factory=lambda config: seen.append(config) or FakeConnector(),
    )

    assert result.inserted == 1
    assert result.account_key == mail_account_key(settings)
    assert "candidate@example.com" not in result.account_key
    assert seen[0].password.get_secret_value() == "secret-value"
    assert store.get_cursor(result.account_key).token == "1"
