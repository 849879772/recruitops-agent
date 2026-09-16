"""Non-semantic mail sanitization. No recruitment classification or field inference."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Sequence

from .models import MailIdentity


_SKIP_HTML_TAGS = frozenset(
    {
        "audio",
        "canvas",
        "embed",
        "iframe",
        "noscript",
        "object",
        "script",
        "style",
        "template",
        "video",
    }
)
_BLOCK_HTML_TAGS = frozenset(
    {
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "pre",
        "section",
        "td",
        "th",
        "tr",
    }
)


class _SafeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.skip_depth = 0
        self.removed_active_content = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _SKIP_HTML_TAGS:
            self.skip_depth += 1
            self.removed_active_content = True
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_HTML_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._save_href(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.skip_depth:
            return
        if tag in _BLOCK_HTML_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._save_href(attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _SKIP_HTML_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in _BLOCK_HTML_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        del data

    def _save_href(self, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.casefold() == "href" and value:
                self.links.append(value)
                break


def _normalise_text(value: str) -> str:
    value = value.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    blank_pending = False
    for line in value.split("\n"):
        cleaned = re.sub(r"[\t\f\v ]+", " ", line).strip()
        if cleaned:
            if blank_pending and lines and lines[-1] != "":
                lines.append("")
            lines.append(cleaned)
            blank_pending = False
        elif lines:
            blank_pending = True
    return "\n".join(lines).strip()


def _sanitise_html(value: str) -> tuple[str, tuple[str, ...], bool]:
    parser = _SafeHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        # Malformed markup is data, not a reason to execute or recover active content.
        return "", (), True
    return _normalise_text("".join(parser.parts)), tuple(parser.links), parser.removed_active_content


def html_to_text(value: str) -> str:
    """Convert untrusted HTML to text without loading or executing any content."""

    return _sanitise_html(value)[0]


safe_html_to_text = html_to_text
strip_html = html_to_text


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)")
_NATIONAL_ID_RE = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{9}[0-9Xx](?!\d)")
_TOKEN_RE = re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{10,}\b|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_KEY_VALUE_RE = re.compile(
    r"(?P<label>\b(?:password|passwd|pwd|token|api[ _-]?key|secret|authorization|cookie|"
    r"验证码|密码|密钥|令牌)\b)\s*(?:[:=：])\s*(?P<value>[^\s,;，；]+)",
    re.IGNORECASE,
)
def redact_sensitive_text(value: str) -> str:
    """Redact common personal identifiers and credentials from mail text."""

    def replace_key_value(match: re.Match[str]) -> str:
        return f"{match.group('label')}=[REDACTED:secret]"

    value = _KEY_VALUE_RE.sub(replace_key_value, value)
    value = _TOKEN_RE.sub("[REDACTED:token]", value)
    value = _EMAIL_RE.sub("[REDACTED:email]", value)
    value = _NATIONAL_ID_RE.sub("[REDACTED:national_id]", value)
    return _PHONE_RE.sub("[REDACTED:phone]", value)


redact_sensitive = redact_sensitive_text


_PROMPT_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_injection_detected",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|earlier)\s+instructions?\b|"
            r"忽略(?:之前|先前|上述|前面)的(?:所有)?(?:指令|指示|提示)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_injection_detected",
        re.compile(
            r"\bsystem\s+prompt\b|\bdeveloper\s+message\b|系统提示|开发者消息",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_injection_detected",
        re.compile(
            r"(?:call|invoke|use)\s+(?:the\s+)?(?:browser|tool|api)|"
            r"(?:调用|使用|执行)(?:浏览器|工具|接口|API)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_injection_detected",
        re.compile(
            r"(?:reveal|show|print|泄露|显示|发送).{0,30}(?:password|api\s*key|token|"
            r"密码|密钥|令牌)",
            re.IGNORECASE,
        ),
    ),
)


def _prompt_flags(text: str) -> list[str]:
    flags: list[str] = []
    for flag, pattern in _PROMPT_INJECTION_RULES:
        if pattern.search(text) and flag not in flags:
            flags.append(flag)
    return flags


def _dedupe_reasons(reasons: Sequence[str]) -> list[str]:
    result: list[str] = []
    for reason in reasons:
        if reason not in result:
            result.append(reason)
    return result


def _redacted_identity(identity: MailIdentity) -> MailIdentity:
    return identity.model_copy(
        update={
            "message_id": redact_sensitive_text(identity.message_id),
            "thread_id": redact_sensitive_text(identity.thread_id)
            if identity.thread_id is not None
            else None,
            "account_ref": redact_sensitive_text(identity.account_ref)
            if identity.account_ref is not None
            else None,
        }
    )


__all__ = [
    "html_to_text", "safe_html_to_text", "strip_html", "redact_sensitive_text", "redact_sensitive",
]
