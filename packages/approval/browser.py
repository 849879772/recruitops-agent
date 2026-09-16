from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.urls import normalize_http_page_url

from .models import ApprovalStatus, OperationName
from .service import ApprovalRegistry


class BrowserActionName(StrEnum):
    OPEN_JOB_DETAIL = "open_job_detail"
    FILTER = "filter"
    NEXT_PAGE = "next_page"
    READ_APPLICATION_STATUS = "read_application_status"


_SELECTOR_KEYS = {
    BrowserActionName.OPEN_JOB_DETAIL: "job_detail_link",
    BrowserActionName.FILTER: "job_filter",
    BrowserActionName.NEXT_PAGE: "next_page",
    BrowserActionName.READ_APPLICATION_STATUS: "application_status",
}


class BrowserActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(min_length=1, max_length=128)
    approval_token: str = Field(min_length=1, max_length=2048)
    consume: Literal[True]
    user_gesture: bool
    action: BrowserActionName
    selector_key: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    tab_id: int = Field(ge=0)
    origin: str = Field(min_length=1, max_length=2048)
    page_url: str | None = Field(default=None, max_length=2048)
    allowed_origins: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_fixed_protocol(self) -> "BrowserActionRequest":
        if self.selector_key != _SELECTOR_KEYS[self.action]:
            raise ValueError("selector key does not match the fixed action protocol")
        parsed = urlparse(self.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("origin must be HTTP(S)")
        normalized_origin = f"{parsed.scheme}://{parsed.netloc}"
        if self.origin.rstrip("/") != normalized_origin.rstrip("/"):
            raise ValueError("origin must not contain a path, query, or fragment")
        if normalized_origin.rstrip("/") not in {
            value.rstrip("/") for value in self.allowed_origins
        }:
            raise ValueError("origin is not in the extension allowlist")
        if self.page_url is not None:
            normalized_page = normalize_http_page_url(self.page_url)
            if normalized_page is None:
                raise ValueError("page URL must be HTTP(S)")
            object.__setattr__(self, "page_url", normalized_page)
        if self.action is BrowserActionName.READ_APPLICATION_STATUS and not self.page_url:
            raise ValueError("application status reads require an exact page URL")
        if self.action is BrowserActionName.FILTER:
            if set(self.params) != {"query"}:
                raise ValueError("filter requires only a query parameter")
            query = self.params.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError("filter query is invalid")
        elif self.params:
            raise ValueError("this browser action does not accept parameters")
        return self


class BrowserActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    status: Literal["consumed"]
    request_id: str
    action: BrowserActionName
    review_id: str | None = None
    target_id: str | None = None
    action_attempt_id: str | None = None
    application_ids: list[str] | None = None


def consume_browser_action(
    registry: ApprovalRegistry,
    request: BrowserActionRequest,
) -> BrowserActionDecision:
    token = registry.token(request.approval_token)
    preview = registry.preview(request.approval_token)
    if token.operation is not OperationName.BROWSER_ACTION:
        raise PermissionError("approval token is not bound to a browser action")
    expected = preview.payload
    if not request.user_gesture and expected.get("command_authorized") is not True:
        raise PermissionError("browser action requires a user gesture or explicit command authorization")
    binding = {
        "action": request.action.value,
        "selector_key": request.selector_key,
        "origin": request.origin.rstrip("/"),
        "params": request.params,
    }
    if request.action is BrowserActionName.READ_APPLICATION_STATUS:
        binding["page_url"] = request.page_url
    for key, value in binding.items():
        expected_value = expected.get(key)
        if key == "origin" and isinstance(expected_value, str):
            expected_value = expected_value.rstrip("/")
        if expected_value != value:
            raise PermissionError(f"browser action binding mismatch: {key}")
    if expected.get("tab_id") is not None and int(expected["tab_id"]) != request.tab_id:
        raise PermissionError("browser action binding mismatch: tab_id")
    decision = registry.consume(request.approval_token)
    if not decision.allowed or decision.status is not ApprovalStatus.CONSUMED:
        code = decision.error_code.value if decision.error_code else "browser_action_not_authorized"
        raise PermissionError(code)
    return BrowserActionDecision(
        allowed=True,
        status="consumed",
        request_id=request.request_id,
        action=request.action,
        review_id=(
            str(expected["review_id"])
            if expected.get("review_id") is not None
            else None
        ),
        target_id=(
            str(expected["target_id"])
            if expected.get("target_id") is not None
            else None
        ),
        action_attempt_id=(
            request.request_id
            if request.action is BrowserActionName.READ_APPLICATION_STATUS
            else None
        ),
        application_ids=(
            [str(item) for item in expected.get("application_ids") or []]
            if request.action is BrowserActionName.READ_APPLICATION_STATUS
            else None
        ),
    )


__all__ = [
    "BrowserActionDecision",
    "BrowserActionName",
    "BrowserActionRequest",
    "consume_browser_action",
]
