"""Pure security decisions used by the stage 8 offline security tests.

The functions in this module do not execute tools, inspect a browser, or make
network calls. They provide fail-closed decisions that an adapter can apply at
its boundary before untrusted page data or a tool request is acted upon.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any


class BrowserPauseReason(StrEnum):
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_REQUIRED = "captcha_required"
    STATE_UNCLEAR = "state_unclear"


class BrowserResumeAction(StrEnum):
    LOGIN = "resume_after_login"
    CAPTCHA = "resume_after_captcha"
    CONFIRM_STATE = "confirm_state"


_RESUME_ACTIONS = {
    BrowserPauseReason.LOGIN_REQUIRED: BrowserResumeAction.LOGIN,
    BrowserPauseReason.CAPTCHA_REQUIRED: BrowserResumeAction.CAPTCHA,
    BrowserPauseReason.STATE_UNCLEAR: BrowserResumeAction.CONFIRM_STATE,
}
_DEFAULT_PAUSE_MESSAGES = {
    BrowserPauseReason.LOGIN_REQUIRED: "User login is required before the task can continue.",
    BrowserPauseReason.CAPTCHA_REQUIRED: (
        "User must complete the CAPTCHA before the task can continue."
    ),
    BrowserPauseReason.STATE_UNCLEAR: "The page state is unclear; user confirmation is required.",
}


@dataclass(frozen=True)
class BrowserPauseState:
    """The fail-closed pause payload shared with the browser protocol."""

    reason: BrowserPauseReason
    resume_action: BrowserResumeAction
    message: str
    status: str = "paused"
    requires_user_action: bool = True

    def to_protocol_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason.value,
            "requiresUserAction": self.requires_user_action,
            "resumeAction": self.resume_action.value,
            "message": self.message,
        }


def build_browser_pause(
    reason: BrowserPauseReason | str,
    message: str | None = None,
) -> BrowserPauseState:
    """Build a protocol-compatible pause; unsupported reasons are rejected."""

    try:
        pause_reason = BrowserPauseReason(reason)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported browser pause reason: {reason!r}") from exc

    raw_message = (
        message
        if isinstance(message, str) and message.strip()
        else _DEFAULT_PAUSE_MESSAGES[pause_reason]
    )
    normalized_message = " ".join(raw_message.split())[:512]
    normalized_message = str(redact_sensitive(normalized_message))
    return BrowserPauseState(
        reason=pause_reason,
        resume_action=_RESUME_ACTIONS[pause_reason],
        message=normalized_message,
    )


class WebContentRisk(StrEnum):
    CLEAN = "clean"
    PROMPT_INJECTION = "prompt_injection"
    MALICIOUS_PAGE = "malicious_page"


@dataclass(frozen=True)
class WebContentAssessment:
    """A deterministic classification of untrusted page text.

    Clean content may be used as passive evidence. No page content is trusted
    as an instruction; a non-clean result is blocked from Agent action.
    """

    risk: WebContentRisk
    matched_rules: tuple[str, ...]
    scanned_length: int
    truncated: bool

    @property
    def blocked(self) -> bool:
        return self.risk is not WebContentRisk.CLEAN

    @property
    def allowed_for_agent(self) -> bool:
        return not self.blocked

    @property
    def safe_for_data(self) -> bool:
        return True


MAX_WEB_CONTENT_LENGTH = 20_000
_WEB_RULES: tuple[tuple[str, WebContentRisk, re.Pattern[str]], ...] = (
    (
        "ignore_prior_instructions",
        WebContentRisk.PROMPT_INJECTION,
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[\s\S]{0,120}"
            r"\b(?:previous|prior|system|developer|user|all)\b[\s\S]{0,40}"
            r"\b(?:instructions?|rules?|messages?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ignore_prior_instructions_zh",
        WebContentRisk.PROMPT_INJECTION,
        re.compile(
            r"(?:忽略|无视|忘记|跳过)[\s\S]{0,40}"
            r"(?:之前|上面|系统|开发者|用户|所有)[\s\S]{0,20}"
            r"(?:指令|提示|规则|消息)",
        ),
    ),
    (
        "tool_or_command_override",
        WebContentRisk.PROMPT_INJECTION,
        re.compile(
            r"\b(?:call|invoke|use|run|execute)\b[\s\S]{0,80}"
            r"\b(?:browser|tool|function|command|shell|script)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration_instruction",
        WebContentRisk.PROMPT_INJECTION,
        re.compile(
            r"\b(?:reveal|show|print|dump|exfiltrate|send|upload|copy)\b[\s\S]{0,80}"
            r"\b(?:passwords?|cookies?|tokens?|api[\s_-]?keys?|secrets?|otps?|private keys?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration_instruction_zh",
        WebContentRisk.PROMPT_INJECTION,
        re.compile(
            r"(?:显示|泄露|发送|上传|复制|打印)[\s\S]{0,50}"
            r"(?:密码|cookie|令牌|密钥|验证码|私钥|秘密)",
        ),
    ),
    (
        "safety_control_bypass",
        WebContentRisk.MALICIOUS_PAGE,
        re.compile(
            r"\b(?:bypass|disable|solve|enter|submit)\b[\s\S]{0,60}"
            r"\b(?:captcha|login|2fa|mfa|waf|safety checks?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_harvest",
        WebContentRisk.MALICIOUS_PAGE,
        re.compile(
            r"\b(?:enter|submit|paste|send|upload)\b[\s\S]{0,80}"
            r"\b(?:passwords?|otps?|one[- ]time passwords?|verification codes?|"
            r"cookies?|tokens?|api keys?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "active_html_or_script",
        WebContentRisk.MALICIOUS_PAGE,
        re.compile(
            r"<\s*(?:script|iframe|object|embed)\b|javascript\s*:|"
            r"on(?:error|load|click|submit)\s*=",
            re.IGNORECASE,
        ),
    ),
)


def assess_web_content(value: str) -> WebContentAssessment:
    """Classify page content without interpreting it as an instruction."""

    if not isinstance(value, str):
        raise TypeError("web content must be a string")

    scanned = value[:MAX_WEB_CONTENT_LENGTH]
    matched_rules: list[str] = []
    highest_risk = WebContentRisk.CLEAN
    for rule_id, rule_risk, pattern in _WEB_RULES:
        if pattern.search(scanned):
            matched_rules.append(rule_id)
            if rule_risk is WebContentRisk.MALICIOUS_PAGE:
                highest_risk = WebContentRisk.MALICIOUS_PAGE
            elif highest_risk is WebContentRisk.CLEAN:
                highest_risk = rule_risk

    if len(value) > len(scanned):
        matched_rules.append("content_truncated")
        highest_risk = WebContentRisk.MALICIOUS_PAGE

    return WebContentAssessment(
        risk=highest_risk,
        matched_rules=tuple(matched_rules),
        scanned_length=len(scanned),
        truncated=len(value) > len(scanned),
    )


READ_ONLY_TOOL_NAMES = frozenset(
    {
        "today_schedule",
        "search_jobs",
        "job_detail",
        "company_coverage",
        "application_query",
        "application_status_review",
        "browser_observation",
        "crawler_acceptance",
        "recruitment_mail_search",
        "recruitment_mail_detail",
        "recruitment_mail_review",
        "schedule_window",
        "edge_connection_status",
        "browser_operation_status",
        "daily_recruitment_sync_status",
    }
)
BROWSER_SIDE_EFFECT_TOOL_NAMES = frozenset(
    {
        "observe_application_status_page",
        "verify_application_status_evidence",
        "cancel_browser_operation",
    }
)
# The browser bridge actions are side-effectful but are not approval-token
# operations: they create or cancel a durable bridge command.
SIDE_EFFECT_TOOL_NAMES = BROWSER_SIDE_EFFECT_TOOL_NAMES
APPROVAL_GATED_WRITE_NAMES = frozenset(
    {
        "company_config_update",
        "application_create",
        "application_stage_update",
        "schedule_create",
    }
)


class ToolAuthorizationReason(StrEnum):
    ALLOWED = "allowed"
    UNKNOWN_TOOL = "unknown_tool"
    NOT_READ_ONLY = "not_read_only"
    SIDE_EFFECT_REQUESTED = "side_effect_requested"


@dataclass(frozen=True)
class ToolAuthorization:
    tool_name: str
    allowed: bool
    reason: ToolAuthorizationReason


def authorize_tool_call(
    tool_name: str,
    *,
    read_only: bool,
    side_effect: bool = False,
) -> ToolAuthorization:
    """Authorize a known query or explicitly classified browser action.

    Approval-gated writes are intentionally outside this capability. Their
    approval policy must be checked by the existing approval layer and a
    separate write executor; this helper only grants the dedicated browser
    bridge action capability when its side effect is declared.
    """

    normalized_name = tool_name.strip().casefold() if isinstance(tool_name, str) else ""
    if normalized_name in BROWSER_SIDE_EFFECT_TOOL_NAMES:
        if read_only:
            return ToolAuthorization(
                tool_name=normalized_name,
                allowed=False,
                reason=ToolAuthorizationReason.NOT_READ_ONLY,
            )
        if not side_effect:
            return ToolAuthorization(
                tool_name=normalized_name,
                allowed=False,
                reason=ToolAuthorizationReason.SIDE_EFFECT_REQUESTED,
            )
        return ToolAuthorization(
            tool_name=normalized_name,
            allowed=True,
            reason=ToolAuthorizationReason.ALLOWED,
        )
    if normalized_name not in READ_ONLY_TOOL_NAMES:
        return ToolAuthorization(
            tool_name=normalized_name,
            allowed=False,
            reason=ToolAuthorizationReason.UNKNOWN_TOOL,
        )
    if side_effect:
        return ToolAuthorization(
            tool_name=normalized_name,
            allowed=False,
            reason=ToolAuthorizationReason.SIDE_EFFECT_REQUESTED,
        )
    if not read_only:
        return ToolAuthorization(
            tool_name=normalized_name,
            allowed=False,
            reason=ToolAuthorizationReason.NOT_READ_ONLY,
        )
    return ToolAuthorization(
        tool_name=normalized_name,
        allowed=True,
        reason=ToolAuthorizationReason.ALLOWED,
    )


class UnauthorizedToolError(PermissionError):
    """Raised by the optional fail-closed tool guard."""


def require_read_only_tool(
    tool_name: str,
    *,
    side_effect: bool = False,
) -> str:
    """Return a normalized read-only name or fail before execution."""

    decision = authorize_tool_call(
        tool_name,
        read_only=True,
        side_effect=side_effect,
    )
    if not decision.allowed:
        raise UnauthorizedToolError(
            f"tool call denied: {decision.reason.value} ({decision.tool_name or '<empty>'})"
        )
    return decision.tool_name


class SensitiveDataError(ValueError):
    """Raised when a boundary payload still contains detectable secrets."""


_REDACTION_RE = re.compile(r"^\[REDACTED:[a-z_]+\]$")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]{1,80} PRIVATE KEY-----[\s\S]*?-----END [^-]{1,80} PRIVATE KEY-----",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+[^\s,;]+",
    re.IGNORECASE,
)
_COOKIE_HEADER_RE = re.compile(r"\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+", re.IGNORECASE)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?P<label>"
    r"api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|"
    r"approval[\s_-]?token|token(?:[\s_-]?id)?|authorization|password|passwd|"
    r"secret|otp|one[\s_-]?time(?:[\s_-]?password)?|verification[\s_-]?code|"
    r"cookie|session(?:[\s_-]?id)?|csrf[\s_-]?token|验证码"
    r")\s*[:=]\s*[\"']?(?P<value>(?!\[REDACTED:[a-z_]+\])[^,\s;\"'}]+)",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|"
    r"password|secret|otp|code)=[^&#\s]+)",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{20,})\b"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}")
_NATIONAL_ID_RE = re.compile(r"\b\d{17}[\dXx]\b")


def _sensitive_key_kind(key: object) -> str | None:
    if not isinstance(key, str):
        return None
    if key.strip() == "验证码":
        return "verification_code"
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    exact = {
        "api_key": "api_key",
        "access_token": "access_token",
        "refresh_token": "refresh_token",
        "approval_token": "approval_token",
        "token": "token",
        "token_id": "token",
        "authorization": "authorization",
        "password": "password",
        "passwd": "password",
        "secret": "secret",
        "otp": "otp",
        "one_time_password": "otp",
        "verification_code": "verification_code",
        "cookie": "cookie",
        "set_cookie": "cookie",
        "session": "session",
        "session_id": "session",
        "csrf_token": "csrf_token",
        "验证码": "verification_code",
    }
    if normalized in exact:
        return exact[normalized]
    if normalized.endswith("_token") and normalized not in {"token_input", "token_output"}:
        return "token"
    return None


def _redaction(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _redact_text(value: str) -> str:
    value = _PRIVATE_KEY_RE.sub(_redaction("private_key"), value)
    value = _AUTH_HEADER_RE.sub(_redaction("authorization"), value)
    value = _COOKIE_HEADER_RE.sub(_redaction("cookie"), value)

    def replace_key_value(match: re.Match[str]) -> str:
        kind = _sensitive_key_kind(match.group("label")) or "secret"
        return f"{match.group('label')}={_redaction(kind)}"

    value = _KEY_VALUE_SECRET_RE.sub(replace_key_value, value)
    value = _QUERY_SECRET_RE.sub(lambda match: _redaction("query_secret"), value)
    value = _KNOWN_TOKEN_RE.sub(_redaction("token"), value)
    value = _JWT_RE.sub(_redaction("token"), value)
    value = _EMAIL_RE.sub(_redaction("email"), value)
    value = _NATIONAL_ID_RE.sub(_redaction("national_id"), value)
    return _PHONE_RE.sub(_redaction("phone"), value)


def redact_sensitive(value: Any) -> Any:
    """Recursively redact common secrets and personal identifiers in payloads."""

    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, child in value.items():
            kind = _sensitive_key_kind(key)
            result[key] = _redaction(kind) if kind else redact_sensitive(child)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, set):
        return {redact_sensitive(item) for item in value}
    return value


_SENSITIVE_VALUE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PRIVATE_KEY_RE),
    ("authorization", _AUTH_HEADER_RE),
    ("cookie", _COOKIE_HEADER_RE),
    ("secret", _KEY_VALUE_SECRET_RE),
    ("query_secret", _QUERY_SECRET_RE),
    ("token", _KNOWN_TOKEN_RE),
    ("token", _JWT_RE),
    ("email", _EMAIL_RE),
    ("phone", _PHONE_RE),
    ("national_id", _NATIONAL_ID_RE),
)


def _is_redacted_value(value: object) -> bool:
    return isinstance(value, str) and bool(_REDACTION_RE.fullmatch(value))


def sensitive_kinds(value: Any) -> tuple[str, ...]:
    """Return secret categories found without returning secret values."""

    found: list[str] = []

    def add(kind: str) -> None:
        if kind not in found:
            found.append(kind)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_kind = _sensitive_key_kind(key)
                if key_kind and not _is_redacted_value(child):
                    add(key_kind)
                visit(child)
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
            return
        if isinstance(item, str):
            for kind, pattern in _SENSITIVE_VALUE_RULES:
                if pattern.search(item):
                    add(kind)

    visit(value)
    return tuple(found)


def contains_sensitive_data(value: Any) -> bool:
    return bool(sensitive_kinds(value))


def require_redacted(value: Any) -> Any:
    """Return a payload only when no detectable sensitive data remains."""

    kinds = sensitive_kinds(value)
    if kinds:
        raise SensitiveDataError("sensitive data detected: " + ", ".join(kinds))
    return value


__all__ = [
    "APPROVAL_GATED_WRITE_NAMES",
    "BROWSER_SIDE_EFFECT_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "SIDE_EFFECT_TOOL_NAMES",
    "BrowserPauseReason",
    "BrowserPauseState",
    "BrowserResumeAction",
    "SensitiveDataError",
    "ToolAuthorization",
    "ToolAuthorizationReason",
    "UnauthorizedToolError",
    "WebContentAssessment",
    "WebContentRisk",
    "assess_web_content",
    "authorize_tool_call",
    "build_browser_pause",
    "contains_sensitive_data",
    "redact_sensitive",
    "require_read_only_tool",
    "require_redacted",
    "sensitive_kinds",
]
