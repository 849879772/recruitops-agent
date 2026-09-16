"""Bounded, incremental mailbox refresh used by all mail reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from threading import Event, Lock
from time import monotonic
from typing import Any

from packages.config import Settings

from .connectors import MailConnectorError
from .runtime import MailRuntimeConfigurationError, sync_configured_mail
from .storage import RecruitmentMailStore


DEFAULT_MAIL_SYNC_TIMEOUT_SECONDS = 30.0
DEFAULT_MAIL_LOCK_WAIT_SECONDS = 1.0

_lock = Lock()
_last_success: dict[tuple[str, object | None], float] = {}
_last_result: dict[tuple[str, object | None], dict[str, Any]] = {}


@dataclass
class _InFlight:
    done: Event = field(default_factory=Event)
    result: dict[str, Any] | None = None


_inflight: dict[tuple[str, object | None], _InFlight] = {}


def _account_key(settings: Settings) -> str:
    material = "\0".join(
        str(getattr(settings, name, ""))
        for name in ("mail_imap_host", "mail_imap_username", "mail_imap_mailbox")
    ).encode("utf-8")
    return sha256(material).hexdigest()[:24]


def _store_identity(store: RecruitmentMailStore) -> object | None:
    """Return a stable process-local identity for an Agent-owned store.

    Lightweight test doubles often do not expose a storage engine, so their
    object identity is used. Real mail stores are isolated by engine identity,
    including memory databases that share the same URL text.
    """

    storage = getattr(store, "storage", None)
    engine = getattr(storage, "engine", None)
    if engine is None:
        return (type(store), id(store))
    try:
        hash(engine)
    except TypeError:
        return (type(engine), id(engine))
    return engine


def _cache_key(settings: Settings, store: RecruitmentMailStore) -> tuple[str, object | None]:
    return _account_key(settings), _store_identity(store)


def _seconds(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(result) or result <= 0:
        return default
    return result


def _sync_timeout(settings: Settings, override: float | None) -> float:
    return _seconds(
        override
        if override is not None
        else getattr(settings, "mail_sync_timeout_seconds", DEFAULT_MAIL_SYNC_TIMEOUT_SECONDS),
        DEFAULT_MAIL_SYNC_TIMEOUT_SECONDS,
    )


def _lock_wait_timeout(settings: Settings, override: float | None, sync_timeout: float) -> float:
    configured = _seconds(
        override
        if override is not None
        else getattr(
            settings,
            "mail_sync_lock_wait_seconds",
            getattr(settings, "mail_sync_lock_timeout_seconds", DEFAULT_MAIL_LOCK_WAIT_SECONDS),
        ),
        DEFAULT_MAIL_LOCK_WAIT_SECONDS,
    )
    return min(configured, sync_timeout)


def _safe_error(exc: Exception) -> str:
    # Configuration errors are useful to the operator; connector errors must
    # never expose host credentials or message bodies.
    if isinstance(exc, MailRuntimeConfigurationError):
        return str(exc)[:256]
    if isinstance(exc, MailConnectorError):
        return str(exc)[:256] or type(exc).__name__
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    return type(exc).__name__


def _failure(
    previous: dict[str, Any] | None,
    error_type: str,
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "sync": {},
        "synced_at": (previous or {}).get("synced_at"),
        "error_type": error_type,
        "timed_out": timed_out,
    }


def _acquire_state_lock(timeout: float) -> bool:
    if timeout <= 0:
        return _lock.acquire(blocking=False)
    return _lock.acquire(timeout=timeout)


def ensure_mail_fresh(
    settings: Settings,
    store: RecruitmentMailStore,
    *,
    limit: int = 100,
    force: bool = False,
    sync_timeout_seconds: float | None = None,
    lock_wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Refresh mail at most once per TTL and return JSON-safe freshness data.

    Only the short state transition is protected by ``_lock``. The actual
    synchronous IMAP call is singleflighted per account/store and receives a
    cooperative deadline; a competing reader never waits indefinitely and no
    timeout worker is left behind.
    """

    if not bool(getattr(settings, "mail_enabled", False)):
        return {"status": "disabled", "sync": {}, "synced_at": None}

    key = _cache_key(settings, store)
    ttl = max(0, int(getattr(settings, "mail_sync_ttl_seconds", 300)))
    sync_timeout = _sync_timeout(settings, sync_timeout_seconds)
    wait_timeout = _lock_wait_timeout(settings, lock_wait_seconds, sync_timeout)
    deadline = monotonic() + sync_timeout

    if not _acquire_state_lock(min(wait_timeout, max(0.0, deadline - monotonic()))):
        return _failure(None, "freshness_lock_timeout", timed_out=True)
    try:
        now = monotonic()
        previous = _last_result.get(key)
        if (
            not force
            and previous is not None
            and previous.get("status") == "synced"
            and now - _last_success.get(key, 0) < ttl
        ):
            return {**previous, "status": "cached"}

        flight = _inflight.get(key)
        owner = flight is None
        if owner:
            flight = _InFlight()
            _inflight[key] = flight
    finally:
        _lock.release()

    if not owner:
        assert flight is not None
        remaining = min(wait_timeout, max(0.0, deadline - monotonic()))
        if not flight.done.wait(remaining):
            return _failure(previous, "mail_sync_in_progress", timed_out=True)
        result = flight.result
        if result is None:
            return _failure(previous, "mail_sync_in_progress", timed_out=True)
        if result.get("status") == "synced":
            return {**result, "status": "cached"}
        return dict(result)

    assert flight is not None
    try:
        result = sync_configured_mail(
            settings,
            store,
            limit=limit,
            timeout_seconds=sync_timeout,
        )
        if monotonic() > deadline:
            raise TimeoutError("mail sync exceeded its total timeout")
        sync = result.model_dump(mode="json")
        payload: dict[str, Any] = {
            "status": "synced",
            "sync": sync,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "error_type": None,
            "timed_out": False,
        }
        with _lock:
            _last_success[key] = monotonic()
            _last_result[key] = payload
            flight.result = payload
            _inflight.pop(key, None)
            flight.done.set()
        return payload
    except Exception as exc:
        error_type = _safe_error(exc)
        payload = _failure(
            previous,
            error_type,
            timed_out=isinstance(exc, TimeoutError) or error_type == "imap_sync_timeout",
        )
        with _lock:
            # Failed refreshes are deliberately not cached. A later caller
            # may retry, while waiters receive this concrete failure outcome.
            flight.result = payload
            _inflight.pop(key, None)
            flight.done.set()
        return payload


def clear_freshness_cache() -> None:
    """Clear process-local successful freshness state for tests and restarts."""

    with _lock:
        _last_success.clear()
        _last_result.clear()


__all__ = [
    "clear_freshness_cache",
    "ensure_mail_fresh",
]
