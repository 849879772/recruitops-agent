from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse


def normalize_http_page_url(value: str) -> str | None:
    """Return a secret-free page identity while retaining functional SPA routes."""

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or re.search(r"[\s\\]", parsed.hostname)
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    authority = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        authority = f"{authority}:{port}"
    path = (parsed.path or "/").rstrip("/") or "/"
    base = f"{scheme}://{authority}{path}"

    fragment = parsed.fragment.split("?", 1)[0]
    if fragment.startswith("/") or fragment.startswith("!/"):
        if len(fragment) <= 1_024 and not re.search(r"[\s#]", fragment):
            return f"{base}#{fragment}"
    return base


def application_progress_channel(record_url: str | None) -> Literal["official_page", "mail_only"]:
    """Classify automated follow-up from the saved progress URL, not job/source URLs.

    This is derived rather than persisted so old records and newly edited links
    immediately follow the same policy without a database migration.
    """

    if isinstance(record_url, str) and normalize_http_page_url(record_url.strip()) is not None:
        return "official_page"
    return "mail_only"


__all__ = ["normalize_http_page_url", "application_progress_channel"]
