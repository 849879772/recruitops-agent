"""Validated persistence for sanitized GiveMeOC browser snapshots."""

from __future__ import annotations

import hashlib
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OC_SOURCE_URL = "https://www.givemeoc.com/"
MAX_OC_RECORDS = 10_000


class OcCaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


OcEntryKind = Literal[
    "form",
    "article",
    "application_record",
    "success_page",
    "third_party_listing",
    "login_page",
]


def normalize_oc_destination_url(value: str) -> str:
    """Normalize one captured destination without changing its routing semantics."""

    normalized = html.unescape(str(value or "")).strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return normalized
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return normalized
    authority = host
    if port and not (
        (parsed.scheme.casefold() == "https" and port == 443)
        or (parsed.scheme.casefold() == "http" and port == 80)
    ):
        authority = f"{authority}:{port}"
    return parsed._replace(
        scheme=parsed.scheme.casefold(),
        netloc=authority,
    ).geturl()


class OcExcludedApplyUrl(OcCaptureModel):
    url: str = Field(min_length=8, max_length=2_048)
    kind: OcEntryKind
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("excluded recruitment links must contain HTTP(S) URLs")
        if parsed.username or parsed.password:
            raise ValueError("excluded recruitment links must not contain credentials")
        return parsed.geturl()


def classify_oc_destination_url(value: str) -> tuple[OcEntryKind, str] | None:
    """Classify final destinations that cannot provide a reusable job list."""

    value = normalize_oc_destination_url(value)
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold().rstrip("/")
    fragment = parsed.fragment.casefold()
    def is_host(domain: str) -> bool:
        return hostname == domain or hostname.endswith(f".{domain}")

    if hostname == "wj.qq.com" or (
        hostname == "docs.qq.com" and path.startswith("/form")
    ):
        return "form", "Tencent questionnaire/form is not a reusable recruitment job list."
    if hostname == "office.chaoxing.com" and "/apps/forms/" in f"{path}/":
        return "form", "Chaoxing application form is not a reusable recruitment job list."
    if any(is_host(domain) for domain in (
        "wjx.cn", "wjx.top", "wjx.com", "jsj.top", "jsjform.com",
        "wenjuan.com", "wenjuan.net", "wj.toutiao.com",
    )) or hostname in {
        "jinshuju.net",
        "www.jinshuju.net",
        "jinshuju.com",
        "www.jinshuju.com",
        "forms.office.com",
        "f.wps.cn",
        "yunbiz.wps.cn",
    } or (hostname == "docs.google.com" and path.startswith("/forms")):
        return "form", "Third-party form is not a reusable recruitment job list."
    if hostname == "alidocs.dingtalk.com" and "/notable/share/form" in path:
        return "form", "DingTalk form is not a reusable recruitment job list."
    if hostname == "distribute.ebiaoge.com" and "/sp/formreport/" in f"{path}/":
        return "form", "Online form report is not a reusable recruitment job list."
    if hostname == "doc.weixin.qq.com" and "smartsheet" in path:
        return "form", "WeChat smart sheet is not a reusable recruitment job list."
    if is_host("mp.weixin.qq.com") or is_host("mp.weixinbridge.com"):
        return "article", "WeChat article is a notice, not a reusable recruitment job list."
    if hostname == "open.weixin.qq.com" and "oauth" in path:
        return "login_page", "WeChat OAuth requires interactive authorization."
    if is_host("zhipin.com") or is_host("liepin.com") or is_host("nowcoder.com") or is_host("wondercv.com"):
        return "third_party_listing", "Third-party job aggregation is not an official reusable recruitment list."
    if path.endswith("/login.html") or path.endswith("/login"):
        return "login_page", "Login pages cannot enumerate public recruitment jobs."
    combined_location = f"{path}#{fragment}"
    if any(
        marker in combined_location
        for marker in (
            "candidatehome/applications",
            "application-record",
            "application_record",
            "delivery-record",
            "delivery_record",
            "recommendation-apply",
            "recommendation_apply",
        )
    ):
        return "application_record", "Application record/personal center cannot enumerate jobs."
    if any(
        marker in combined_location
        for marker in ("apply-success", "apply_success", "application-success")
    ):
        return "success_page", "Application success page cannot enumerate jobs."
    return None


class OcCaptureRecord(OcCaptureModel):
    company: str = Field(min_length=1, max_length=200)
    company_type: str = Field(default="", max_length=80)
    industry: str = Field(default="", max_length=500)
    recruitment_type: str = Field(default="", max_length=200)
    recruitment_target: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=500)
    position: str = Field(default="", max_length=500)
    status: str = Field(default="", max_length=100)
    update_time: str = Field(default="", max_length=100)
    deadline: str = Field(default="", max_length=100)
    apply_urls: list[str] = Field(default_factory=list, max_length=20)
    resolved_apply_urls: list[str] = Field(default_factory=list, max_length=20)
    excluded_apply_urls: list[OcExcludedApplyUrl] = Field(default_factory=list, max_length=20)
    link_resolution: str = Field(default="not_requested", max_length=40)
    notice: str = Field(default="", max_length=1_000)
    exam_info: str = Field(default="", max_length=1_000)
    company_size: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=1_000)

    @field_validator("apply_urls", "resolved_apply_urls")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("recruitment links must contain HTTP(S) URLs")
            if parsed.username or parsed.password:
                raise ValueError("recruitment links must not contain credentials")
            clean = normalize_oc_destination_url(parsed.geturl())
            if clean not in result:
                result.append(clean)
        return result


class OcLinkResolutionSummary(OcCaptureModel):
    requested: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)
    resolved: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)
    unresolved: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)
    login_fallbacks: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)
    login_required: bool = False
    addressable: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)
    excluded: int = Field(default=0, ge=0, le=MAX_OC_RECORDS * 20)

    @model_validator(mode="after")
    def validate_counts(self) -> "OcLinkResolutionSummary":
        if self.resolved + self.unresolved != self.requested:
            raise ValueError("resolved and unresolved link counts must sum to requested")
        if self.login_fallbacks > self.unresolved:
            raise ValueError("login_fallbacks cannot exceed unresolved links")
        if self.login_required and self.resolved > 0:
            raise ValueError("login_required cannot be true after a link resolved")
        classified = self.addressable + self.excluded
        if classified not in {0, self.resolved}:
            raise ValueError("addressable and excluded counts must sum to resolved")
        return self


class OcCaptureRequest(OcCaptureModel):
    source_url: str = Field(default=OC_SOURCE_URL, min_length=8, max_length=2_048)
    captured_at: datetime
    total_pages: int = Field(ge=1, le=100)
    total_items: int = Field(ge=1, le=MAX_OC_RECORDS)
    page_counts: list[int] = Field(min_length=1, max_length=100)
    records: list[OcCaptureRecord] = Field(min_length=1, max_length=MAX_OC_RECORDS)
    link_resolution: OcLinkResolutionSummary = Field(default_factory=OcLinkResolutionSummary)

    @model_validator(mode="after")
    def validate_pagination_evidence(self) -> "OcCaptureRequest":
        if len(self.page_counts) != self.total_pages:
            raise ValueError("page_counts must contain one entry per captured page")
        if any(count < 1 for count in self.page_counts):
            raise ValueError("captured OC pages must not be empty")
        if sum(self.page_counts) != self.total_items:
            raise ValueError("page_counts must sum to total_items")
        if len(self.records) != self.total_items:
            raise ValueError("records must contain every advertised OC item")
        return self

    @field_validator("source_url")
    @classmethod
    def validate_source(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "www.givemeoc.com":
            raise ValueError("source_url must be the GiveMeOC HTTPS origin")
        return OC_SOURCE_URL

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        return value


class OcCaptureResult(OcCaptureModel):
    snapshot_path: str
    record_count: int = Field(ge=1)
    total_pages: int = Field(ge=1)
    total_items: int = Field(ge=1)
    captured_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    link_resolution: OcLinkResolutionSummary = Field(default_factory=OcLinkResolutionSummary)


def persist_oc_capture(request: OcCaptureRequest, path: str | Path) -> OcCaptureResult:
    """Atomically replace the latest sanitized OC snapshot."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for record in request.records:
        item = record.model_dump(mode="json")
        addressable: list[str] = []
        excluded = list(record.excluded_apply_urls)
        excluded_urls = {entry.url for entry in excluded}
        for value in record.resolved_apply_urls:
            classification = classify_oc_destination_url(value)
            if classification is None:
                addressable.append(value)
                continue
            if value not in excluded_urls:
                kind, reason = classification
                excluded.append(OcExcludedApplyUrl(url=value, kind=kind, reason=reason))
                excluded_urls.add(value)
        item["resolved_apply_urls"] = addressable
        item["excluded_apply_urls"] = [entry.model_dump(mode="json") for entry in excluded]
        if excluded and not addressable and record.link_resolution == "resolved":
            item["link_resolution"] = "excluded_non_job_entry"
        records.append(item)

    payload: dict[str, Any] = {
        "source_url": OC_SOURCE_URL,
        "captured_at": request.captured_at.astimezone(timezone.utc).isoformat(),
        "filters": {
            "company_types": ["民企"],
            "target_candidates": "2027",
            "recruitment_types": ["秋招", "秋招提前批"],
        },
        "pagination": {
            "total_pages": request.total_pages,
            "total_items": request.total_items,
            "page_counts": request.page_counts,
            "complete": True,
        },
        "link_resolution": request.link_resolution.model_dump(mode="json"),
        "records": records,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return OcCaptureResult(
        snapshot_path=str(destination),
        record_count=len(request.records),
        total_pages=request.total_pages,
        total_items=request.total_items,
        captured_at=request.captured_at,
        sha256=digest,
        link_resolution=request.link_resolution,
    )


__all__ = [
    "MAX_OC_RECORDS",
    "OC_SOURCE_URL",
    "OcCaptureRecord",
    "OcExcludedApplyUrl",
    "OcEntryKind",
    "OcLinkResolutionSummary",
    "OcCaptureRequest",
    "OcCaptureResult",
    "persist_oc_capture",
    "classify_oc_destination_url",
]
