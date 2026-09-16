from __future__ import annotations

from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator

from packages.security import (
    BrowserPauseReason,
    assess_web_content,
    redact_sensitive,
)

from .typed import (
    EvidenceSource,
    ToolErrorCode,
    ToolInput,
    ToolModel,
    ToolResponse,
    ToolStatus,
)


class BrowserObservationStatus(StrEnum):
    OBSERVED = "observed"
    PAUSED = "paused"
    BLOCKED = "blocked"


class NetworkRequestObservation(ToolModel):
    """Metadata-only request evidence; headers, bodies and cookies are forbidden."""

    method: str = Field(min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=2048)
    status_code: int | None = Field(default=None, ge=100, le=599)
    resource_type: str | None = Field(default=None, max_length=80)

    @field_validator("url")
    @classmethod
    def validate_request_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("network observation requires an HTTP(S) URL")
        return value


class BrowserObservationInput(ToolInput):
    """A sanitized observation supplied by an approved browser adapter."""

    url: str = Field(min_length=1, max_length=2048)
    allowed_origins: list[str] = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=500)
    page_text: str = Field(default="", max_length=20_000)
    links: list[str] = Field(default_factory=list, max_length=200)
    network_requests: list[NetworkRequestObservation] = Field(
        default_factory=list,
        max_length=500,
    )
    pause_reason: BrowserPauseReason | None = None

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("browser observation requires an HTTP(S) URL")
        return value


class BrowserObservationData(ToolModel):
    url: str
    origin: str
    title: str
    text: str
    links: list[str]
    network_requests: list[NetworkRequestObservation]
    status: BrowserObservationStatus
    risk_rules: list[str] = Field(default_factory=list)
    pause_reason: BrowserPauseReason | None = None


class BrowserObservationResponse(ToolResponse[BrowserObservationData]):
    pass


def observe_browser_page(
    request: BrowserObservationInput,
    repository: Any = None,
) -> BrowserObservationResponse:
    """Validate a passive browser observation; never navigates or clicks."""

    del repository
    parsed = urlparse(request.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = {str(item).rstrip("/") for item in request.allowed_origins}
    started_url = request.url
    evidence = [EvidenceSource(source="browser_dom", source_ref=started_url)]
    if origin not in allowed:
        return BrowserObservationResponse(
            tool_name="browser_observation",
            status=ToolStatus.FAILURE,
            success=False,
            data=None,
            evidence=evidence,
            error_code=ToolErrorCode.INVALID_SOURCE,
            error_message="The observed origin is not in the approved allowlist.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )

    if request.pause_reason is not None:
        data = BrowserObservationData(
            url=request.url,
            origin=origin,
            title=str(redact_sensitive(request.title)),
            text="",
            links=[],
            network_requests=[],
            status=BrowserObservationStatus.PAUSED,
            pause_reason=request.pause_reason,
        )
        return BrowserObservationResponse(
            tool_name="browser_observation",
            status=ToolStatus.FAILURE,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message=f"Browser paused: {request.pause_reason.value}.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )

    assessment = assess_web_content(request.page_text)
    safe_text = str(redact_sensitive(request.page_text))
    safe_links = []
    for link in request.links:
        parsed_link = urlparse(link)
        if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc:
            continue
        safe_link = parsed_link._replace(
            params="",
            query="",
            fragment="",
        ).geturl()
        safe_links.append(str(redact_sensitive(safe_link)))
    safe_requests = []
    for item in request.network_requests:
        parsed_request = urlparse(item.url)
        safe_url = parsed_request._replace(query="", fragment="").geturl()
        safe_requests.append(item.model_copy(update={"url": safe_url}))
    data = BrowserObservationData(
        url=request.url,
        origin=origin,
        title=str(redact_sensitive(request.title)),
        text=safe_text,
        links=safe_links,
        network_requests=safe_requests,
        status=(
            BrowserObservationStatus.BLOCKED
            if assessment.blocked
            else BrowserObservationStatus.OBSERVED
        ),
        risk_rules=list(assessment.matched_rules),
    )
    if assessment.blocked:
        return BrowserObservationResponse(
            tool_name="browser_observation",
            status=ToolStatus.FAILURE,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.UNTRUSTED_WEB_CONTENT,
            error_message="Untrusted page content is blocked from Agent instructions.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )
    return BrowserObservationResponse(
        tool_name="browser_observation",
        status=ToolStatus.SUCCESS,
        success=True,
        data=data,
        evidence=evidence,
        timeout_ms=request.timeout_ms,
        elapsed_ms=0,
        read_only=True,
    )


__all__ = [
    "BrowserObservationData",
    "BrowserObservationInput",
    "BrowserObservationResponse",
    "BrowserObservationStatus",
    "NetworkRequestObservation",
    "observe_browser_page",
]
