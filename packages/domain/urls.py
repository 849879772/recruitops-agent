from __future__ import annotations

import re
from urllib.parse import urlparse


def normalize_http_page_url(value: str) -> str | None:
    """Return a secret-free page identity while retaining functional SPA routes."""

    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
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


__all__ = ["normalize_http_page_url"]
