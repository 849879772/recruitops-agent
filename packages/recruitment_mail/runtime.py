from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import Protocol

from packages.config import Settings

from .connectors import (
    DEFAULT_IMAP_TIMEOUT_SECONDS,
    ImapConnectionConfig,
    ImapReadOnlyConnector,
    MailConnector,
)
from .storage import RecruitmentMailStore
from .sync import RecruitmentMailSyncResult, RecruitmentMailSyncService


class MailRuntimeConfigurationError(RuntimeError):
    pass


class ConnectorFactory(Protocol):
    def __call__(self, config: ImapConnectionConfig) -> MailConnector: ...


def mail_account_key(settings: Settings) -> str:
    material = (
        f"{settings.mail_imap_host.casefold()}\0"
        f"{settings.mail_imap_username.casefold()}"
    ).encode("utf-8")
    return f"imap-{sha256(material).hexdigest()[:24]}"


def sync_configured_mail(
    settings: Settings,
    store: RecruitmentMailStore,
    *,
    connector_factory: ConnectorFactory = ImapReadOnlyConnector,
    limit: int = 100,
    run_id: str | None = None,
    operation_id: str | None = None,
    attempts: int = 1,
    timeout_seconds: float | None = None,
) -> RecruitmentMailSyncResult:
    if not settings.mail_enabled:
        raise MailRuntimeConfigurationError("recruitment_mail_disabled")
    required = {
        "host": settings.mail_imap_host,
        "username": settings.mail_imap_username,
        "password": settings.mail_imap_password,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise MailRuntimeConfigurationError(
            f"recruitment_mail_missing_configuration:{','.join(missing)}"
        )
    config = ImapConnectionConfig(
        host=settings.mail_imap_host,
        port=settings.mail_imap_port,
        username=settings.mail_imap_username,
        password=settings.mail_imap_password,
        mailbox=settings.mail_imap_mailbox,
        timeout_seconds=(
            DEFAULT_IMAP_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    connector = connector_factory(config)
    return RecruitmentMailSyncService(store).sync(
        account_key=mail_account_key(settings),
        connector=connector,
        mailbox=settings.mail_imap_mailbox,
        limit=limit,
        run_id=run_id,
        operation_id=operation_id,
        attempts=attempts,
    )


__all__ = [
    "MailRuntimeConfigurationError",
    "mail_account_key",
    "sync_configured_mail",
]
