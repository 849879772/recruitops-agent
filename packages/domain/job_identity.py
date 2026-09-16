from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_KEYS = {
    "from", "ref", "referrer", "recommendcode", "shareid", "source", "spm",
}


def normalize_job_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text)


def normalize_job_identity_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().removeprefix("www.")
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and not ((parsed.scheme.casefold() == "https" and port == 443) or (parsed.scheme.casefold() == "http" and port == 80)):
        host = f"{host}:{port}"
    query = [
        (key.casefold(), item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
    ]
    fragment = parsed.fragment
    if "?" in fragment:
        route, fragment_query = fragment.split("?", 1)
        kept = [
            (key.casefold(), item)
            for key, item in parse_qsl(fragment_query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
        ]
        fragment = route + (f"?{urlencode(sorted(kept))}" if kept else "")
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), host, path, urlencode(sorted(query)), fragment))


@dataclass(frozen=True, slots=True)
class JobIdentity:
    business_key: str
    stable_id: str
    native_job_id: str
    normalized_detail_url: str


def build_job_identity(company: Mapping[str, Any], job: Mapping[str, Any]) -> JobIdentity:
    """Build a location-independent identity while respecting recruitment units."""

    organization_id = str(company.get("organization_id") or company.get("id") or "").strip()
    unit_id = str(company.get("recruitment_unit_id") or company.get("id") or "").strip()
    title = normalize_job_title(job.get("title"))
    normalized_url = normalize_job_identity_url(job.get("detail_url") or job.get("jd_url"))
    department = normalize_job_title(job.get("department") or job.get("job_family"))
    if not organization_id or not title:
        raise ValueError("organization and job title are required for job identity")
    if normalized_url:
        identity_parts = ["v1", organization_id, title, normalized_url]
    else:
        identity_parts = ["v1", organization_id, unit_id, title, department]
    encoded = json.dumps(identity_parts, ensure_ascii=False, separators=(",", ":"))
    business_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return JobIdentity(
        business_key=business_key,
        stable_id=f"job-{business_key}",
        native_job_id=str(job.get("native_job_id") or job.get("id") or "").strip(),
        normalized_detail_url=normalized_url,
    )


__all__ = [
    "JobIdentity",
    "build_job_identity",
    "normalize_job_identity_url",
    "normalize_job_title",
]
