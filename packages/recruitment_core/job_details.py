"""On-demand job-detail hydration for sparse crawler records."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import time
import unicodedata
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .crawlers.hotjob import fetch_hotjob_position_detail
from .crawlers.render import render_page
from .jd_capture import assess_jd_capture

logger = logging.getLogger(__name__)

_DETAIL_SIGNAL_RE = re.compile(
    r"职位描述|岗位描述|职位职责|岗位职责|工作职责|任职要求|岗位要求|"
    r"任职资格|招聘要求|加分项|responsibilities|requirements|qualifications",
    re.I,
)
_PUBLISHED_ONLY_RE = re.compile(r"发布于\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")
_EMPTY_DETAIL_SHELL_RE = re.compile(
    r"^(?:岗位职责|职位描述|岗位描述|职位职责)\s*"
    r"(?:岗位要求|任职要求|任职资格|招聘要求)\s*"
    r"(?:工作地点|部门意向|申请|分享|收藏)(?:\s|$)",
    re.I,
)
_START_MARKERS = (
    "职位描述", "岗位描述", "职位职责", "岗位职责", "工作职责", "工作内容",
    "职位介绍", "Job Description", "Responsibilities",
)
_END_MARKERS = (
    "职位信息", "公司信息", "公司介绍", "企业介绍", "相关推荐", "相似职位",
    "申请职位", "投递职位", "Apply Now",
)
_NUMBERED_ITEM_RE = re.compile(
    r"(?:^|[\n\r；;])\s*(?:[-*•·]|\(?\d{1,2}\)?[.、:：)）])\s*\S+",
    re.M,
)
_DUTY_LANGUAGE_RE = re.compile(
    r"负责|参与|设计|开发|研发|维护|优化|测试|部署|实现|构建|推进|协作|解决|支持"
)
_REQUIREMENT_LANGUAGE_RE = re.compile(
    r"熟悉|掌握|具备|能够|本科|硕士|博士|专业|经验|能力|优先|了解"
)
_LIST_SHELL_METADATA_RE = re.compile(
    r"发布于|工作地点|招聘类型|职位类别|所属部门|部门意向|城市|校招|校园招聘|"
    r"全职|申请|投递|收藏|分享"
)
_VISIBLE_LOGIN_TEXT_RE = re.compile(
    r"请先登录|登录后(?:查看|继续|投递)|手机登录|密码登录|登录招聘系统",
    re.I,
)
_VISIBLE_CAPTCHA_TEXT_RE = re.compile(
    r"安全验证|人机验证|滑动验证|captcha|challenge",
    re.I,
)
_ERROR_PAGE_HEADING_RE = re.compile(
    r"""^(?:
        [45]\d\d(?:\s*[-:：|丨].*)?
        |error(?:\s*[-:：|丨].*)?
        |not\s+found(?:\s*[-:：|丨].*)?
        |page\s+not\s+found(?:\s*[-:：|丨].*)?
        |嗯\s*[.…\.]{1,3}\s*无法访问此页面
        |无法访问此页面
        |页面不存在
        |找不到页面
        |请求失败
        |服务器错误
        |页面出错
    )$""",
    re.I | re.X,
)
_REMAINING_DETAIL_CONTROL_RE = re.compile(
    r"加载中|正在加载|loading|read\s*more|show\s*more|load\s*more|"
    r"展开|更多|显示更多|查看详情|…|\.{3,}",
    re.I,
)
_DIAGNOSTIC_TEXT_LIMIT = 240
_DIAGNOSTIC_ITEMS_LIMIT = 20
_SAFE_MALFORMED_URL = "[malformed-url]"
_DIAGNOSTIC_SENSITIVE_BODY_RE = re.compile(
    r"(?i)\b((?:request|response)[_-]?body|body)\b[\"']?\s*[:=].*"
)
_DIAGNOSTIC_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(authorization|auth|cookie|password|passwd|secret|session(?:[_-]?id)?|"
    r"(?:access|refresh)[_-]?token|token|api[_-]?key|signature|sig"
    r")\b[\"']?"
    r"\s*[:=]\s*[\"']?[^\s,;&}\"']+"
)
_LIST_RENDER_TIMEOUT_MS = 30000
_LIST_RENDER_WAIT_MS = 1800


@dataclass(frozen=True, slots=True)
class JobDetailHydrationResult:
    """One non-throwing JD hydration outcome for batch diagnostics."""

    detail: str
    status: str
    source: str = ""
    detail_url: str = ""
    attempts: tuple[str, ...] = ()
    error_type: str = ""
    error_detail: str = ""
    identity_status: str = ""
    identity_evidence: tuple[str, ...] = ()
    identity_diagnostic: dict[str, object] = field(default_factory=dict)
    capture_evidence: dict[str, object] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.status == "complete"


def _capture_sha256(detail: str) -> str:
    normalized = str(detail or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _bounded_diagnostic_text(value: object, limit: int = _DIAGNOSTIC_TEXT_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _redact_diagnostic_text(value: object) -> str:
    text = _bounded_diagnostic_text(value)
    if not text:
        return ""
    redacted = _DIAGNOSTIC_SENSITIVE_BODY_RE.sub(r"\1=<redacted>", text)
    redacted = _DIAGNOSTIC_SENSITIVE_VALUE_RE.sub(r"\1=<redacted>", redacted)
    return _bounded_diagnostic_text(redacted)


def _sensitive_diagnostic_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").casefold()).strip("_")
    compact = normalized.replace("_", "")
    return (
        compact in {
            "authorization", "auth", "cookie", "password", "passwd", "secret",
            "session", "sessionid", "token", "apikey", "accesstoken",
            "refreshtoken", "signature", "sig", "body", "requestbody",
            "responsebody",
        }
        or compact.endswith(("token", "signature", "sig", "secret", "body"))
    )


def _safe_diagnostic_url(value: object) -> str:
    """Keep route/tenant context while removing credential-bearing query keys."""

    try:
        raw = str(value or "")
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
        if not parsed.scheme or not hostname:
            return _SAFE_MALFORMED_URL if raw else ""
        safe_netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            safe_netloc = f"[{hostname}]"
        if port is not None:
            safe_netloc = f"{safe_netloc}:{port}"
        safe_query = urlencode([
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not _sensitive_diagnostic_key(key)
        ])
        safe_fragment = _redact_diagnostic_text(parsed.fragment)
        safe_url = urlunsplit((parsed.scheme, safe_netloc, parsed.path, safe_query, safe_fragment))
    except (TypeError, ValueError):
        return _SAFE_MALFORMED_URL
    if not raw:
        return ""
    return _redact_diagnostic_text(safe_url)


def _safe_exception_detail(exc: BaseException | None) -> str:
    """Keep a short exception message without persisting common credentials."""

    return _redact_diagnostic_text(exc)


def _capture_method(source: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    if source.endswith("_api") or source in {
        "feishu_api", "lenovo_official_api", "moka_official", "moka_provenance",
        "huawei_api", "beisen_api", "tencent_api", "hotjob_api",
    }:
        return "official_api"
    if source in {"render", "configured_page_render", "moka_direct"}:
        return "detail_dom"
    return source or ""


def _capture_evidence(
    *,
    detail: str,
    status: str,
    source: str,
    detail_url: str,
    identity_status: str,
    method: str = "",
    terminal_observed: bool = False,
    remaining_controls: object = None,
    identity_diagnostic: Mapping | None = None,
    load_state: str = "",
) -> dict[str, object]:
    controls = list(remaining_controls or []) if isinstance(remaining_controls, (list, tuple, set)) else []
    identity_verified = identity_status in {"matched", "request_bound"}
    evidence = {
        "status": "complete" if status == "complete" and detail and identity_verified and terminal_observed and not controls else "incomplete" if detail or status in {
            "content_incomplete", "list_url", "detail_link", "official_unavailable", "official_sparse",
        } else "unknown",
        "method": _capture_method(source, method),
        "source_url": detail_url,
        "identity_verified": identity_verified,
        "terminal_observed": bool(terminal_observed),
        "remaining_controls": controls,
        "content_sha256": _capture_sha256(detail),
    }
    if identity_diagnostic:
        evidence["identity_diagnostic"] = dict(identity_diagnostic)
    if load_state:
        evidence["load_state"] = load_state
    # Stored/cache receipts bypass this builder, so only fresh complete captures get a timestamp.
    if evidence["status"] == "complete":
        evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
    return evidence


def _capture_metadata(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.find("html") or soup
    raw_controls = root.get("data-recruitops-remaining-controls") or "[]"
    try:
        controls = json.loads(raw_controls)
    except (TypeError, ValueError):
        controls = []
    return {
        "status": str(root.get("data-recruitops-capture-status") or "unknown"),
        "method": str(root.get("data-recruitops-capture-method") or ""),
        "terminal_observed": str(root.get("data-recruitops-terminal-observed") or "").casefold() == "true",
        "remaining_controls": controls if isinstance(controls, list) else [],
        "final_url": str(root.get("data-recruitops-final-url") or "").strip(),
        "load_state": str(root.get("data-recruitops-load-state") or "unknown").strip().casefold(),
    }


def _has_structured_untitled_jd(raw_text: str, remainder: str) -> bool:
    if len(remainder) < 220:
        return False
    clauses = [
        item.strip()
        for item in re.split(r"(?:\r?\n|[；;。])", raw_text)
        if item.strip()
    ]
    semantic_clauses = [
        item
        for item in clauses
        if _DUTY_LANGUAGE_RE.search(item) or _REQUIREMENT_LANGUAGE_RE.search(item)
    ]
    has_multiple_items = (
        len(_NUMBERED_ITEM_RE.findall(raw_text)) >= 3
        or len(semantic_clauses) >= 4
    )
    return bool(
        has_multiple_items
        and _DUTY_LANGUAGE_RE.search(remainder)
        and _REQUIREMENT_LANGUAGE_RE.search(remainder)
    )


def _is_metadata_only_shell(text: str) -> bool:
    return bool(
        len(_LIST_SHELL_METADATA_RE.findall(text)) >= 3
        and not _DUTY_LANGUAGE_RE.search(text)
        and not _REQUIREMENT_LANGUAGE_RE.search(text)
    )


def is_jd_incomplete(job: dict) -> bool:
    """Return True when stored text is only a list-card summary or is blank."""
    raw_text = str(job.get("jd_raw") or "")
    text = " ".join(raw_text.split())
    if not text:
        return True
    title = " ".join(str(job.get("title") or "").split())
    remainder = text.replace(title, "", 1).strip(" -|:：") if title else text
    remainder = _PUBLISHED_ONLY_RE.sub("", remainder).strip(" -|:：")
    if _EMPTY_DETAIL_SHELL_RE.search(remainder) or _is_metadata_only_shell(remainder):
        return True
    has_detail_signal = bool(_DETAIL_SIGNAL_RE.search(text))
    if has_detail_signal and len(remainder) >= 50:
        return False
    if _has_structured_untitled_jd(raw_text, remainder):
        return False
    return True


def _is_hidden_control(node) -> bool:
    for item in (node, *node.parents):
        if item.has_attr("hidden") or item.get("aria-hidden") == "true":
            return True
        if item.get("data-recruitops-visible") == "false":
            return True
        if re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
            item.get("style", ""),
            re.I,
        ):
            return True
    return False


def _scope_remaining_controls(scope) -> list[str]:
    """Find visible expansion/loading controls inside the selected job scope."""

    selector = (
        "button, a, [role='button'], [role='tab'], [aria-expanded='false'], "
        "[aria-busy='true'], [data-loading='true'], [role='progressbar'], "
        ".loading, .is-loading, [class*='ellipsis'], [class*='expand'], [class*='more']"
    )
    controls: list[str] = []
    for node in scope.select(selector):
        if _is_hidden_control(node):
            continue
        label = " ".join(
            str(node.get(attribute) or "").strip()
            for attribute in ("aria-label", "title")
        ).strip()
        text = " ".join(node.get_text(" ", strip=True).split())
        value = label or text
        classes = " ".join(node.get("class") or [])
        is_loading = bool(
            node.get("aria-busy") == "true"
            or node.get("data-loading") == "true"
            or node.get("role") == "progressbar"
            or re.search(r"(?:^|[-_ ])(?:loading|is-loading)(?:$|[-_ ])", classes, re.I)
        )
        is_collapsed = node.get("aria-expanded") == "false"
        is_named_remaining = bool(_REMAINING_DETAIL_CONTROL_RE.search(value or classes))
        if not (is_loading or is_collapsed or is_named_remaining):
            continue
        marker = value or "loading" if is_loading else value or node.name
        if marker not in controls:
            controls.append(marker[:120])
    return controls[:20]


def _access_control_status(html: str) -> str:
    """Classify visible login/CAPTCHA gates without treating hidden widgets as gates."""

    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.select(
        "iframe[src*='captcha'], iframe[src*='challenge'], "
        "form[action*='captcha'], #captcha, #challenge-form"
    ):
        if not _is_hidden_control(node):
            return "captcha_required"
    for node in soup.select("input[type='password']"):
        if not _is_hidden_control(node):
            return "login_required"

    visible_text = " ".join(
        node.get_text(" ", strip=True)
        for node in soup.find_all(["title", "h1", "h2", "h3", "form"])
        if not _is_hidden_control(node)
    )
    if _VISIBLE_CAPTCHA_TEXT_RE.search(visible_text):
        return "captcha_required"
    if _VISIBLE_LOGIN_TEXT_RE.search(visible_text):
        return "login_required"
    return ""


_IDENTITY_FAILURES = {"identity_mismatch", "identity_ambiguous"}
_ID_FIELDS = ("id", "jobId", "job_id", "postId", "positionId", "jobAdId")
_TITLE_FIELDS = ("title", "jobTitle", "jobName", "positionName", "postName", "JobAdName", "jobAdName")
_COMPANY_FIELDS = ("company", "companyName", "company_name", "hiringOrganization")
_BEISEN_REQUEST_ID_FIELDS = ("Id", "id")
_BEISEN_JOB_AD_ID_FIELDS = ("JobAdId", "jobAdId")
_BEISEN_TENANT_ID_FIELDS = ("TenantId", "tenantId", "OrgId", "orgId")
_BEISEN_EXPECTED_TENANT_FIELDS = (
    "beisen_tenant_id", "tenant_id", "tenantId", "source_tenant_id", "org_id", "orgId",
)
_BEISEN_HOST_FIELDS = (
    "source_tenant_host", "company_campus_host", "company_host",
    "company_campus_url", "campus_url", "careers_url", "source_url",
    "source_list_url", "list_url",
)
_MOKA_DISPLAY_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:急|急聘|hot)\s*(?:[|｜:：·•\-]\s*|\s+)",
    re.I,
)
_TENCENT_POST_ID_FIELDS = ("postId", "post_id")
_TENCENT_INTERNAL_ID_FIELDS = ("id", "jobId", "job_id")
_TENCENT_TITLE_FIELDS = (*_TITLE_FIELDS, "name")


def _identity_key(value: object) -> str:
    # Keep language punctuation: C, C++ and C# are different identities.
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _moka_title_identity_key(value: object) -> str:
    """Compare Moka titles without treating a leading display badge as content."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _MOKA_DISPLAY_TITLE_PREFIX_RE.sub("", text, count=1)
    return _identity_key(text)


def _is_error_page_heading(value: object) -> bool:
    """Exclude browser error headings from the job-title candidate set."""

    text = " ".join(str(value or "").split()).strip()
    return bool(text and _ERROR_PAGE_HEADING_RE.fullmatch(text))


def _url_job_id(url: str) -> str:
    parsed = urlsplit(url)
    for query in (parsed.query, parsed.fragment.partition("?")[2]):
        for name, value in parse_qsl(query):
            if name.casefold() in {"id", "jobid", "jobadid", "postid", "positionid", "advertisementid"}:
                return value
    for path in (parsed.fragment.partition("?")[0], parsed.path):
        match = re.search(r"/(?:job|jobs|position|posts)/(?!list(?:/|$)|detail(?:/|$))([^/]+)", path, re.I)
        if match:
            return unquote(match.group(1))
    return ""


def _diagnostic_route_job_id(url: str) -> str:
    """Extract a request route ID for diagnostics without widening identity checks."""

    try:
        route_id = _url_job_id(url)
    except (TypeError, ValueError):
        return ""
    if route_id:
        return route_id
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    for path in (parsed.fragment.partition("?")[0], parsed.path):
        match = re.search(r"/zpdetail/([^/?#]+)", path, re.I)
        if match:
            return unquote(match.group(1))
    return ""


def _is_reproducible_synthetic_id(job: Mapping) -> bool:
    native_id = str(job.get("native_job_id") or "")
    if not (native_id and native_id == str(job.get("id") or "") and job.get("company_id")):
        return False
    # Older pipeline normalization copies its synthetic row ID into
    # native_job_id. Exclude only an exactly reproducible internal hash,
    # never arbitrary 64-character IDs supplied by an official source.
    values = (
        job["company_id"], job.get("detail_url") or job.get("jd_url"),
        job.get("title"), job.get("city"),
    )
    encoded = "\x00".join(
        " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
        for value in values
    )
    return native_id == hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _requested_id(job: Mapping) -> str:
    native_id = str(job.get("native_job_id") or "")
    if _is_reproducible_synthetic_id(job):
        native_id = ""
    return native_id or str(job.get("source_job_id") or "")


def _check_identity(
    job: Mapping,
    observed: Mapping,
    *,
    requested_id: str = "",
    id_fields: tuple[str, ...] = _ID_FIELDS,
    title_fields: tuple[str, ...] = _TITLE_FIELDS,
    title_normalizer: str = "",
) -> tuple[str, tuple[str, ...]]:
    evidence = []
    mismatch = False
    title_key = _moka_title_identity_key if title_normalizer == "moka" else _identity_key
    for label, expected, fields in (
        ("native_id", _requested_id(job) or requested_id, id_fields),
        ("title", job.get("title"), title_fields),
        ("company", job.get("company") or job.get("company_name"), _COMPANY_FIELDS),
    ):
        if not expected:
            continue
        observed_values = []
        for field in fields:
            value = observed.get(field)
            if isinstance(value, Mapping):
                value = value.get("name")
            if value is None or value == "":
                continue
            evidence.append(f"{label}:{value}")
            observed_values.append(value)
        # Callers must select fields from one identity namespace. Conflicting
        # titles or company aliases must not be hidden by another matching field.
        if observed_values and not all(
            (title_key(value) if label == "title" else _identity_key(value))
            == (title_key(expected) if label == "title" else _identity_key(expected))
            for value in observed_values
        ):
            mismatch = True
    return ("identity_mismatch" if mismatch else ""), tuple(evidence)


def _observed_identity_evidence(observed: Mapping) -> tuple[str, ...]:
    """Expose bounded ID/title observations for identity diagnostics."""

    evidence: list[str] = []
    for field in _ID_FIELDS:
        value = observed.get(field)
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("name")
        if value not in (None, ""):
            evidence.append(f"native_id:{_bounded_diagnostic_text(value)}")
    for field in _TITLE_FIELDS:
        value = observed.get(field)
        if isinstance(value, Mapping):
            value = value.get("name")
        if value not in (None, ""):
            evidence.append(f"title:{_bounded_diagnostic_text(value)}")
    return tuple(dict.fromkeys(evidence))[:_DIAGNOSTIC_ITEMS_LIMIT]


def _merge_identity_evidence(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged = []
    for group in groups:
        for item in group:
            value = _bounded_diagnostic_text(item)
            if value and value not in merged:
                merged.append(value)
            if len(merged) >= _DIAGNOSTIC_ITEMS_LIMIT:
                return tuple(merged)
    return tuple(merged)


def _identity_diagnostic(
    job: Mapping,
    status: str,
    *,
    source: str,
    detail_url: str,
    attempts: tuple[str, ...],
    identity_evidence: tuple[str, ...] = (),
    error_type: str = "",
    error_detail: str = "",
) -> dict[str, object]:
    """Build a bounded identity failure record without changing admission gates."""

    if status not in _IDENTITY_FAILURES:
        return {}
    requested_job_id = _requested_id(job)
    requested_route_job_id = _diagnostic_route_job_id(detail_url)
    request_bound_ids: list[str] = []
    observed_job_ids: list[str] = []
    observed_titles: list[str] = []
    for item in identity_evidence:
        raw = _bounded_diagnostic_text(item)
        prefix, separator, value = raw.partition(":")
        if not separator:
            continue
        if prefix in {"request_id", "job_ad_id"} and ":" in value:
            _, value = value.split(":", 1)
        value = _bounded_diagnostic_text(value)
        if not value:
            continue
        if prefix in {"request_id", "requested_route_job_id", "request_bound_id"}:
            if value not in request_bound_ids:
                request_bound_ids.append(value)
            continue
        if prefix in {
            "native_id", "job_id", "route_id", "job_ad_id", "observed_id",
            "internal_job_id", "post_id", "position_id",
        }:
            if value not in observed_job_ids:
                observed_job_ids.append(value)
        elif prefix in {"title", "job_title", "job_name", "position_name", "post_name"}:
            if value not in observed_titles:
                observed_titles.append(value)
    if requested_route_job_id and requested_route_job_id not in request_bound_ids:
        request_bound_ids.append(_bounded_diagnostic_text(requested_route_job_id))
    final_attempts = attempts or (f"{source}:{status}",)
    return {
        "status": status,
        "requested_job_id": _bounded_diagnostic_text(requested_job_id),
        "requested_title": _bounded_diagnostic_text(job.get("title")),
        "requested_route_job_id": _bounded_diagnostic_text(requested_route_job_id),
        "request_bound_ids": request_bound_ids[:_DIAGNOSTIC_ITEMS_LIMIT],
        "observed_job_ids": observed_job_ids[:_DIAGNOSTIC_ITEMS_LIMIT],
        "observed_titles": observed_titles[:_DIAGNOSTIC_ITEMS_LIMIT],
        "observation_status": "observed" if observed_job_ids or observed_titles else "not_observed",
        "source": _bounded_diagnostic_text(source),
        "url": _safe_diagnostic_url(detail_url),
        "failed_step": _bounded_diagnostic_text(final_attempts[-1]),
        "exception": {
            "type": _bounded_diagnostic_text(error_type),
            "detail": _redact_diagnostic_text(error_detail),
        },
    }


def _beisen_field_values(observed: Mapping, fields: tuple[str, ...]) -> tuple[tuple[str, object], ...]:
    """Collect one Beisen identity namespace without merging field meanings."""

    wanted = {field.casefold() for field in fields}
    values: list[tuple[str, object]] = []
    for field, value in observed.items():
        if str(field).casefold() not in wanted or value in (None, ""):
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend((str(field), item) for item in value if item not in (None, ""))
        else:
            values.append((str(field), value))
    return tuple(values)


def _beisen_bound_identity(identity: Mapping) -> bool:
    return any(
        str(identity.get(field) or "").strip()
        for field in (
            "company_id", "native_job_id", "source_job_id", "source_tenant",
            "company_campus_url", "company_campus_host",
        )
    )


def _beisen_host_binding(url: str, identity: Mapping) -> tuple[str, tuple[str, ...]]:
    detail_host = (urlparse(url).hostname or "").casefold().rstrip(".")
    evidence = [f"detail_host:{detail_host}"] if detail_host else []
    tenant_host = ""
    source_tenant = str(identity.get("source_tenant") or "").strip()
    tenant_match = re.match(r"^beisen(?:_mobile)?:([^:/]+)", source_tenant, re.I)
    if tenant_match:
        tenant_host = tenant_match.group(1).casefold().rstrip(".")
    configured_hosts: set[str] = set()
    for field in _BEISEN_HOST_FIELDS:
        raw = identity.get(field)
        if not raw:
            continue
        parsed = urlparse(str(raw) if "://" in str(raw) else f"https://{raw}")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host:
            configured_hosts.add(host)
    expected_hosts = {tenant_host} if tenant_host else configured_hosts
    if tenant_host and configured_hosts and configured_hosts != {tenant_host}:
        evidence.append(f"expected_hosts:{','.join(sorted({tenant_host, *configured_hosts}))}")
        return "identity_mismatch", (*evidence, "reason:beisen_tenant_host_conflict")
    if expected_hosts:
        evidence.append(f"expected_hosts:{','.join(sorted(expected_hosts))}")
        if detail_host not in expected_hosts:
            return "identity_mismatch", (*evidence, "reason:beisen_host_mismatch")
    return "", tuple(evidence)


def _beisen_identity_check(
    identity: Mapping,
    observed: Mapping,
    *,
    requested_id: str,
    url: str,
    host_evidence: tuple[str, ...] = (),
    request_namespace: str = "request_id",
) -> tuple[str, tuple[str, ...]]:
    """Validate Beisen's route UUID and numeric JobAdId as separate namespaces."""

    bound = _beisen_bound_identity(identity)
    evidence = list(host_evidence)
    request_norm = _identity_key(requested_id)

    route_pairs = _beisen_field_values(observed, _BEISEN_REQUEST_ID_FIELDS)
    route_values = {_identity_key(value) for _, value in route_pairs if _identity_key(value)}
    evidence.extend(f"request_id:{field}:{value}" for field, value in route_pairs)
    evidence.extend(f"observed_id:{value}" for _, value in route_pairs)
    job_ad_pairs = _beisen_field_values(observed, _BEISEN_JOB_AD_ID_FIELDS)
    job_ad_values = {_identity_key(value) for _, value in job_ad_pairs if _identity_key(value)}
    evidence.extend(f"job_ad_id:{field}:{value}" for field, value in job_ad_pairs)
    if len(job_ad_values) > 1:
        return "identity_mismatch", (*evidence, "reason:beisen_job_ad_id_conflict")

    if request_namespace == "job_ad_id":
        if len(job_ad_values) != 1 or request_norm not in job_ad_values:
            return "identity_mismatch", (*evidence, "reason:beisen_request_job_ad_id_mismatch")
    elif route_values:
        if len(route_values) != 1 or request_norm not in route_values:
            return "identity_mismatch", (*evidence, "reason:beisen_request_id_mismatch")
    elif bound:
        return "identity_mismatch", (*evidence, "reason:beisen_request_id_missing")

    bound_ids: list[tuple[str, str]] = []
    native_id = str(identity.get("native_job_id") or "")
    if native_id and not _is_reproducible_synthetic_id(identity):
        bound_ids.append(("native_job_id", native_id))
    source_id = str(identity.get("source_job_id") or "")
    if source_id:
        bound_ids.append(("source_job_id", source_id))
    official_namespaces = {
        "request_id": route_values,
        "job_ad_id": job_ad_values,
    }
    for label, bound_id in bound_ids:
        bound_norm = _identity_key(bound_id)
        matches = [
            namespace
            for namespace, values in official_namespaces.items()
            if bound_norm and bound_norm in values
        ]
        evidence.append(f"bound_id:{label}:{bound_id}")
        if len(matches) != 1:
            return "identity_mismatch", (
                *evidence,
                f"reason:beisen_{label}_conflict",
            )
        evidence.append(f"bound_namespace:{label}:{matches[0]}")

    conflict, generic_evidence = _check_identity(
        identity,
        observed,
        requested_id="",
        id_fields=(),
        title_fields=_TITLE_FIELDS,
    )
    evidence.extend(generic_evidence)
    if conflict:
        return conflict, tuple(evidence)

    title_values = _beisen_field_values(observed, _TITLE_FIELDS)
    if identity.get("title"):
        if not title_values and bound:
            return "identity_mismatch", (*evidence, "reason:beisen_title_missing")
        if title_values and any(
            _identity_key(value) != _identity_key(identity.get("title"))
            for _, value in title_values
        ):
            return "identity_mismatch", (*evidence, "reason:beisen_title_mismatch")

    expected_tenant = _beisen_field_values(identity, _BEISEN_EXPECTED_TENANT_FIELDS)
    observed_tenant = _beisen_field_values(observed, _BEISEN_TENANT_ID_FIELDS)
    expected_tenant_values = {_identity_key(value) for _, value in expected_tenant if _identity_key(value)}
    observed_tenant_values = {_identity_key(value) for _, value in observed_tenant if _identity_key(value)}
    evidence.extend(f"tenant:{field}:{value}" for field, value in observed_tenant)
    if len(observed_tenant_values) > 1:
        return "identity_mismatch", (*evidence, "reason:beisen_tenant_conflict")
    if expected_tenant_values and observed_tenant_values != expected_tenant_values:
        return "identity_mismatch", (*evidence, "reason:beisen_tenant_mismatch")

    return "", tuple(evidence)


class _DetailStatus(tuple):
    """Keep the public (detail, status) tuple while carrying API diagnostics."""

    def __new__(
        cls,
        detail: str,
        status: str,
        *,
        error_type: str = "",
        error_detail: str = "",
        attempts: tuple[str, ...] = (),
        detail_url: str = "",
        identity_status: str = "",
        identity_evidence: tuple[str, ...] = (),
    ):
        result = super().__new__(cls, (detail, status))
        result.error_type = error_type
        result.error_detail = error_detail
        result.attempts = attempts
        result.detail_url = detail_url
        result.identity_status = identity_status
        result.identity_evidence = identity_evidence
        return result


def _api_failure(exc: Exception) -> _DetailStatus:
    return _DetailStatus(
        "", "timeout" if isinstance(exc, requests.Timeout) else "fetch_failed",
        error_type=type(exc).__name__,
        error_detail=_safe_exception_detail(exc),
    )


def _clean_api_text(value: object) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def fetch_feishu_job_description_status(url: str, *, identity: Mapping | None = None) -> tuple[str, str]:
    """Return Feishu JD text and a durable verification status."""
    parsed = urlparse(url)
    match = re.search(r"/position/(\d+)/detail", parsed.path)
    if not match:
        return "", "not_applicable"
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/job/posts/{match.group(1)}"
    try:
        response = requests.get(
            api_url,
            params={"portal_type": 6, "with_recommend": "false"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": url},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        business_code = payload.get("code") if isinstance(payload, Mapping) else None
        if business_code not in (None, 0, "0", 200, "200"):
            return _DetailStatus("", "fetch_failed", detail_url=url)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        detail = data.get("job_post_detail") if isinstance(data, Mapping) else None
        detail = detail if isinstance(detail, Mapping) else {}
        # Feishu post IDs belong to the public detail route; job_id belongs
        # to a separate internal namespace and is evidence, not an alias.
        route_identity = {**(identity or {}), "native_job_id": match.group(1)}
        conflict, identity_evidence = _check_identity(
            route_identity, detail, requested_id=match.group(1), id_fields=("id",)
        )
        if detail.get("job_id") not in (None, ""):
            identity_evidence += (f"internal_job_id:{detail['job_id']}",)
        if conflict:
            return _DetailStatus(
                "", conflict, detail_url=url,
                identity_status=conflict.removeprefix("identity_"),
                identity_evidence=identity_evidence,
            )
        description = _clean_api_text(detail.get("description"))
        requirement = _clean_api_text(detail.get("requirement"))
        parts = []
        if description:
            parts.extend(["岗位职责", description])
        if requirement:
            parts.extend(["任职要求", requirement])
        detail_text = "\n".join(parts)
        if detail_text:
            return _DetailStatus(
                detail_text, "complete", detail_url=url,
                # Preserve the Feishu helper's established request-bound
                # status while exposing the observed API identity below.
                identity_status="request_bound",
                identity_evidence=identity_evidence or (f"request_id:{match.group(1)}",),
            )
        if detail:
            return "", "official_unavailable"
        return "", "fetch_failed"
    except requests.Timeout as exc:
        logger.debug("飞书岗位详情 API 超时 %s: %s", url, exc)
        return _api_failure(exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("飞书岗位详情 API 获取失败 %s: %s", url, exc)
        return _api_failure(exc)


def _fetch_feishu_job_description(url: str) -> str:
    detail, _status = fetch_feishu_job_description_status(url)
    return detail


# Lenovo's public campus portal keeps the detail page as a small SPA shell.
# This branch is intentionally bound to the stored Lenovo host/project before
# it asks the public gateway for the exact numeric job id.
_LENOVO_HOST = "talent.lenovo.com.cn"
_LENOVO_API_ROOT = "https://talent.lenovo.com.cn/gateway/jobBase/list"


def _lenovo_route_id(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != _LENOVO_HOST
        or parsed.path.rstrip("/").casefold() != "/position/detail"
    ):
        return ""
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.casefold() == "id" and re.fullmatch(r"\d+", value or ""):
            return value
    return ""


def _lenovo_is_synthetic_row_id(identity: Mapping) -> bool:
    """Recognize only the catalog's reproducible 64-hex row identity."""

    native_id = str(identity.get("native_job_id") or "").strip().casefold()
    row_id = str(identity.get("id") or "").strip().casefold()
    if not (
        native_id
        and native_id == row_id
        and re.fullmatch(r"[0-9a-f]{64}", native_id)
        and identity.get("company_id")
        and identity.get("detail_url")
    ):
        return False
    values = (
        identity.get("company_id"),
        identity.get("detail_url"),
        identity.get("title"),
        identity.get("city"),
    )
    encoded = "\x00".join(
        " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
        for value in values
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest() == native_id


def _lenovo_identity_binding(
    identity: Mapping,
    route_id: str,
) -> tuple[bool, tuple[str, ...], str]:
    """Bind stored native IDs to the exact numeric ID in the detail URL."""

    evidence: list[str] = []
    native_id = str(identity.get("native_job_id") or "").strip()
    source_id = str(identity.get("source_job_id") or "").strip()
    if native_id:
        if _lenovo_is_synthetic_row_id(identity):
            evidence.append("native_id:synthetic_catalog_row_id")
        elif _identity_key(native_id) != _identity_key(route_id):
            return False, tuple(evidence), "lenovo_native_job_id_mismatch"
        else:
            evidence.append(f"native_id:{native_id}")
    if source_id:
        if _identity_key(source_id) != _identity_key(route_id):
            return False, tuple(evidence), "lenovo_source_job_id_mismatch"
        evidence.append(f"source_job_id:{source_id}")
    evidence.append(f"route_id:{route_id}")
    return True, tuple(evidence), ""


def _lenovo_company_binding(identity: Mapping) -> tuple[bool, tuple[str, ...], str]:
    company = str(identity.get("company") or identity.get("company_name") or "").strip()
    company_url = str(
        identity.get("company_campus_url")
        or identity.get("campus_url")
        or identity.get("careers_url")
        or ""
    ).strip()
    parsed = urlsplit(company_url)
    host = (parsed.hostname or "").casefold()
    normalized_company = _identity_key(company)
    if not company or not ("联想" in company or "lenovo" in normalized_company):
        return False, (), "lenovo_company_identity_untrusted"
    if parsed.scheme != "https" or host != _LENOVO_HOST:
        return False, (), "lenovo_company_host_mismatch"
    project_path = parsed.path.rstrip("/").casefold()
    if project_path != "/position" and not project_path.startswith("/position/"):
        return False, (), "lenovo_project_binding_missing"
    platform = str(
        identity.get("source_platform") or identity.get("company_crawler_key") or ""
    ).strip().casefold()
    if platform and platform != "lenovo":
        return False, (), "lenovo_source_platform_mismatch"
    evidence = (
        f"company:{company}",
        f"company_host:{host}",
        f"project_path:{parsed.path.rstrip('/') or '/'}",
    )
    return True, evidence, ""


def fetch_lenovo_job_description_status(
    url: str,
    *,
    identity: Mapping | None = None,
) -> tuple[str, str]:
    """Fetch one exact Lenovo campus JD through its public detail endpoint."""

    route_id = _lenovo_route_id(url)
    if not route_id:
        return "", "not_applicable"
    bound_identity = dict(identity or {})
    company_ok, company_evidence, company_failure = _lenovo_company_binding(
        bound_identity
    )
    if not company_ok:
        return _DetailStatus(
            "",
            "identity_mismatch",
            detail_url=url,
            identity_status="mismatch",
            identity_evidence=company_evidence or (f"reason:{company_failure}",),
        )

    id_ok, id_evidence, id_failure = _lenovo_identity_binding(
        bound_identity, route_id
    )
    if not id_ok:
        return _DetailStatus(
            "",
            "identity_mismatch",
            detail_url=url,
            identity_status="mismatch",
            identity_evidence=(*company_evidence, *id_evidence, f"reason:{id_failure}"),
        )
    try:
        response = requests.get(
            _LENOVO_API_ROOT,
            params={"jobId": route_id},
            headers={
                "Accept": "application/json",
                "Referer": url,
                "User-Agent": "Mozilla/5.0",
                "portal-type": "PC",
            },
            timeout=20,
        )
        if response.status_code in {401, 403}:
            return _DetailStatus(
                "",
                "access_denied",
                detail_url=url,
                error_type=f"HTTP{response.status_code}",
                identity_status="request_bound",
                identity_evidence=(*company_evidence, *id_evidence),
            )
        if response.status_code == 429:
            return _DetailStatus(
                "",
                "access_denied",
                detail_url=url,
                error_type="HTTP429",
                identity_status="request_bound",
                identity_evidence=(*company_evidence, *id_evidence),
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            return _DetailStatus(
                "", "api_variant_unsupported", detail_url=url,
                identity_status="request_bound",
                identity_evidence=(*company_evidence, *id_evidence),
            )
        code = payload.get("code")
        if code in {910910, "910910", 910004, "910004"}:
            return _DetailStatus(
                "", "login_required", detail_url=url,
                identity_status="request_bound",
                identity_evidence=(*company_evidence, *id_evidence),
            )
        if code not in (None, 0, "0", 200, "200"):
            return _DetailStatus(
                "", "official_unavailable", detail_url=url,
                identity_status="request_bound",
                identity_evidence=(*company_evidence, *id_evidence),
            )
        result = payload.get("result")
        rows = result.get("rows") if isinstance(result, Mapping) else None
        rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        exact_rows = [
            row for row in rows
            if any(
                _identity_key(row.get(field)) == _identity_key(route_id)
                for field in ("id", "jobId")
                if row.get(field) not in (None, "")
            )
        ]
        if len(exact_rows) > 1:
            return _DetailStatus(
                "", "identity_ambiguous", detail_url=url,
                identity_status="ambiguous",
                identity_evidence=(*company_evidence, f"route_id:{route_id}"),
            )
        if not exact_rows:
            observed = rows[0] if rows else {}
            conflict, observed_evidence = _check_identity(
                bound_identity,
                observed,
                requested_id=route_id,
                id_fields=("id", "jobId"),
                title_fields=("jobName", "title"),
            )
            status = conflict or ("official_unavailable" if not rows else "identity_mismatch")
            return _DetailStatus(
                "", status, detail_url=url,
                identity_status=status.removeprefix("identity_") if status in _IDENTITY_FAILURES else "request_bound",
                identity_evidence=(*company_evidence, *id_evidence, *observed_evidence),
            )
        data = exact_rows[0]
        conflict, observed_evidence = _check_identity(
            bound_identity,
            data,
            requested_id=route_id,
            id_fields=("id", "jobId"),
            title_fields=("jobName", "title"),
        )
        identity_evidence = (*company_evidence, *id_evidence, *observed_evidence)
        if conflict:
            return _DetailStatus(
                "", conflict, detail_url=url,
                identity_status=conflict.removeprefix("identity_"),
                identity_evidence=identity_evidence,
            )
        duties = _clean_api_text(data.get("jobDuties"))
        requirements = _clean_api_text(data.get("jobRequirement"))
        parts = []
        if duties:
            parts.extend(["岗位职责", duties])
        if requirements:
            parts.extend(["任职要求", requirements])
        detail = "\n".join(parts)
        if not detail:
            return _DetailStatus(
                "", "official_unavailable", detail_url=url,
                identity_status="matched", identity_evidence=identity_evidence,
            )
        # The official, exact-id response is the completeness proof.  JD
        # richness is intentionally left to downstream analysis.
        status = "complete"
        return _DetailStatus(
            detail,
            status,
            detail_url=url,
            attempts=("lenovo_detail_api:complete",),
            identity_status="matched",
            identity_evidence=identity_evidence,
        )
    except requests.Timeout as exc:
        logger.debug("联想岗位详情 API 超时 %s: %s", url, exc)
        return _api_failure(exc)
    except requests.RequestException as exc:
        logger.debug("联想岗位详情 API 请求失败 %s: %s", url, exc)
        return _DetailStatus(
            "", "fetch_failed", detail_url=url, error_type=type(exc).__name__,
            identity_status="request_bound",
            identity_evidence=(*company_evidence, *id_evidence),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("联想岗位详情 API 响应无效 %s: %s", url, exc)
        return _DetailStatus(
            "", "api_variant_unsupported", detail_url=url, error_type=type(exc).__name__,
            identity_status="request_bound",
            identity_evidence=(*company_evidence, *id_evidence),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("联想岗位详情 API 获取失败 %s: %s", url, exc)
        return _api_failure(exc)


@lru_cache(maxsize=256)
def _moka_site_context(site_url: str) -> tuple[str, int, str]:
    response = requests.get(site_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    node = BeautifulSoup(response.text, "html.parser").select_one("#init-data")
    if node is None:
        raise ValueError("Moka page does not expose init-data")
    raw = node.get("value") or node.get_text("", strip=True)
    decoded = html_lib.unescape(str(raw or ""))
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        payload = {}
    org = payload.get("org") if isinstance(payload.get("org"), dict) else {}
    org_id = str(payload.get("orgId") or org.get("id") or "").strip()
    site_id_value = payload.get("siteId")
    aes_iv = str(payload.get("aesIv") or "").strip()
    if not org_id:
        match = re.search(
            r'"org"\s*:\s*\{.{0,400}?"id"\s*:\s*"([A-Za-z0-9_-]{1,100})"',
            decoded,
            re.S,
        ) or re.search(r'"orgId"\s*:\s*"([A-Za-z0-9_-]{1,100})"', decoded)
        org_id = match.group(1) if match else ""
    if not site_id_value:
        match = re.search(r'"siteId"\s*:\s*"?(\d{1,12})"?', decoded)
        site_id_value = match.group(1) if match else 0
    if not aes_iv:
        match = re.search(r'"aesIv"\s*:\s*"([A-Za-z0-9]{16})"', decoded)
        aes_iv = match.group(1) if match else ""
    site_id = int(site_id_value or 0)
    if not org_id or not site_id or len(aes_iv.encode("utf-8")) != 16:
        raise ValueError("Moka init-data is missing its public site context")
    return org_id, site_id, aes_iv


_MOKA_SITE_PATH_RE = re.compile(
    r"^/(?:m/)?(?:campus-recruitment|campus_apply|apply|social-recruitment|"
    r"recommendation-recruitment|recommendation-apply)/[^/]+/\d+",
    re.I,
)
_MOKA_JOB_ROUTE_RE = re.compile(r"(?:^|/)job/([0-9a-f-]{16,})(?:/|$)", re.I)
_MOKA_LIST_PAGE_SIZE = 50
_MOKA_LIST_MAX_PAGES = 6


class _MokaApiVariantUnsupported(ValueError):
    pass


_MOKA_PRIMARY_DETAIL_FIELDS = ("jobDescription", "description", "content")
_MOKA_REQUIREMENT_FIELDS = (
    "jobRequirements", "requirements", "requirement", "qualifications",
    "任职要求", "岗位要求", "任职资格", "招聘要求",
)


def _moka_text_value(value: object) -> str:
    """Clean only text-bearing Moka fields; never stringify arbitrary JSON."""

    if isinstance(value, str):
        return _clean_api_text(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return _clean_api_text("\n".join(value))
    return ""


def _moka_detail_content(data: Mapping) -> tuple[str, tuple[str, ...]]:
    """Assemble Moka's explicit JD fields and return the fields used as evidence."""

    primary = ""
    used_fields: list[str] = []
    for field in _MOKA_PRIMARY_DETAIL_FIELDS:
        value = _moka_text_value(data.get(field))
        if value:
            primary = value
            used_fields.append(field)
            break

    requirements: list[str] = []
    normalized_primary = " ".join(primary.split())
    for field in _MOKA_REQUIREMENT_FIELDS:
        value = _moka_text_value(data.get(field))
        if not value or " ".join(value.split()) in normalized_primary:
            continue
        requirements.append(value)
        used_fields.append(field)

    if requirements:
        requirement_text = "\n\n任职要求\n" + "\n".join(requirements)
        primary = f"{primary}{requirement_text}" if primary else requirement_text.lstrip()
    return primary, tuple(used_fields)


def _trusted_moka_url(url: str, *, trusted_custom_host: bool) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    official_host = host == "mokahr.com" or host.endswith(".mokahr.com")
    return parsed.scheme == "https" and bool(host) and (official_host or trusted_custom_host)


def _moka_job_id(url: str) -> str:
    parsed = urlsplit(url)
    for route in (parsed.fragment.partition("?")[0], parsed.path):
        match = _MOKA_JOB_ROUTE_RE.search(route)
        if match:
            return match.group(1)
    return ""


def _moka_site_url(url: str, *, trusted_custom_host: bool) -> str:
    if not _trusted_moka_url(url, trusted_custom_host=trusted_custom_host):
        return ""
    parsed = urlsplit(url)
    match = _MOKA_SITE_PATH_RE.match(parsed.path)
    if match is None:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{match.group(0)}"


def _moka_provenance_site(job: Mapping, detail_url: str) -> tuple[str, bool]:
    """Return a Moka site URL only when the job carries same-host list evidence."""

    official_site = _moka_site_url(detail_url, trusted_custom_host=False)
    if official_site:
        return official_site, False
    detail_host = (urlsplit(detail_url).hostname or "").casefold()
    if not detail_host:
        return "", False
    for field in (
        "careers_url", "campaign_url", "source_url", "source_list_url",
        "list_url", "resolved_source_url", "detail_link_source_url",
    ):
        if field == "detail_link_source_url" and job.get("detail_link_observed") is not True:
            continue
        candidate = str(job.get(field) or "").strip()
        if not candidate:
            continue
        site_url = _moka_site_url(candidate, trusted_custom_host=True)
        if site_url and (urlsplit(site_url).hostname or "").casefold() == detail_host:
            return site_url, True
    return "", False


def _moka_job_coordinates(
    url: str,
    *,
    trusted_custom_host: bool = False,
    site_url: str = "",
) -> tuple[str, str] | None:
    if not _trusted_moka_url(url, trusted_custom_host=trusted_custom_host):
        return None
    job_id = _moka_job_id(url)
    resolved_site_url = _moka_site_url(url, trusted_custom_host=trusted_custom_host)
    if not resolved_site_url and site_url:
        resolved_site_url = _moka_site_url(
            site_url,
            trusted_custom_host=trusted_custom_host,
        )
        if (
            resolved_site_url
            and urlsplit(resolved_site_url).hostname != urlsplit(url).hostname
        ):
            resolved_site_url = ""
    if not job_id or not resolved_site_url:
        return None
    return resolved_site_url, job_id


def _decode_moka_payload(envelope: object, aes_iv: str) -> dict:
    if not isinstance(envelope, Mapping):
        raise _MokaApiVariantUnsupported("response is not a JSON object")
    data = envelope.get("data")
    key = str(envelope.get("necromancer") or "").encode("utf-8")
    if isinstance(data, Mapping):
        return dict(envelope)
    if not key and any(name in envelope for name in ("code", "success", "msg")):
        return dict(envelope)
    if not isinstance(data, str) or not data or len(key) not in {16, 24, 32}:
        raise _MokaApiVariantUnsupported("response uses an unsupported envelope")
    if len(aes_iv.encode("utf-8")) != 16:
        raise _MokaApiVariantUnsupported("response IV is unavailable")
    try:
        encrypted = b64decode(data, validate=True)
        if not encrypted or len(encrypted) % 16:
            raise ValueError("invalid encrypted payload length")
        decryptor = Cipher(
            algorithms.AES(key), modes.CBC(aes_iv.encode("utf-8"))
        ).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        padding = padded[-1]
        if padding < 1 or padding > 16 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("invalid response padding")
        payload = json.loads(padded[:-padding].decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _MokaApiVariantUnsupported(str(exc)) from exc
    if not isinstance(payload, dict):
        raise _MokaApiVariantUnsupported("decoded response is not a JSON object")
    return payload


def _moka_payload_jobs(payload: Mapping) -> list[Mapping]:
    data = payload.get("data")
    containers = [data, payload]
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("jobs", "jobList", "list", "rows"):
            rows = container.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
    return []


def _fetch_moka_job_from_list(
    site_url: str,
    job_id: str,
    *,
    org_id: str,
    site_id: int,
    aes_iv: str,
    title: str = "",
    identity: Mapping | None = None,
) -> tuple[str, str]:
    parsed = urlsplit(site_url)
    endpoint = f"{parsed.scheme}://{parsed.netloc}/api/outer/ats-apply/website/jobs/v2"
    for page in range(_MOKA_LIST_MAX_PAGES):
        response = requests.post(
            endpoint,
            json={
                "orgId": org_id,
                "siteId": site_id,
                "locale": "zh-CN",
                "limit": _MOKA_LIST_PAGE_SIZE,
                "offset": page * _MOKA_LIST_PAGE_SIZE,
                "needStat": page == 0,
                **({"keyword": title} if title else {}),
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": site_url},
            timeout=20,
        )
        response.raise_for_status()
        payload = _decode_moka_payload(response.json(), aes_iv)
        rows = _moka_payload_jobs(payload)
        matched_rows = [row for row in rows if str(row.get("id") or row.get("jobId") or "").casefold() == job_id.casefold()]
        if len(matched_rows) > 1:
            return "", "identity_ambiguous"
        for row in matched_rows:
            conflict, _ = _check_identity(
                identity or {"title": title},
                row,
                requested_id=job_id,
                title_normalizer="moka",
            )
            if conflict:
                return "", conflict
            detail, _detail_fields = _moka_detail_content(row)
            if not detail:
                return "", "official_unavailable"
            status = "complete"
            return detail, status
        if len(rows) < _MOKA_LIST_PAGE_SIZE:
            break
    return "", "official_unavailable"


def _fetch_moka_direct_page(url: str, title: str = "", *, identity: Mapping | None = None) -> tuple[str, str]:
    parsed = urlsplit(url)
    if not _moka_job_id(url):
        return "", "not_applicable"
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    result = _extract_scoped_jd(
        response.text, identity or {"title": title},
        detail_url=str(getattr(response, "url", "") or url), source="moka_direct",
    )
    return _DetailStatus(
        result.detail,
        result.status,
        attempts=result.attempts,
        detail_url=result.detail_url,
        error_type=result.error_type,
        error_detail=result.error_detail,
        identity_status=result.identity_status,
        identity_evidence=result.identity_evidence,
    )


def fetch_moka_job_description_status(
    url: str,
    *,
    trusted_custom_host: bool = False,
    site_url: str = "",
    title: str = "",
    identity: Mapping | None = None,
) -> tuple[str, str]:
    """Fetch one public Moka job detail through bounded official-only fallbacks."""

    coordinates = _moka_job_coordinates(
        url,
        trusted_custom_host=trusted_custom_host,
        site_url=site_url,
    )
    if coordinates is None:
        if _trusted_moka_url(url, trusted_custom_host=trusted_custom_host) and _moka_job_id(url):
            try:
                return _fetch_moka_direct_page(url, title, identity=identity)
            except requests.Timeout as exc:
                return _api_failure(exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Moka 直接详情获取失败 %s: %s", url, exc)
                return _api_failure(exc)
        return "", "not_applicable"
    resolved_site_url, job_id = coordinates
    primary_status = "fetch_failed"
    attempts: list[str] = []
    error_type = ""
    error_detail = ""
    try:
        org_id, site_id, aes_iv = _moka_site_context(resolved_site_url)
        parsed = urlsplit(resolved_site_url)
        response = requests.post(
            f"{parsed.scheme}://{parsed.netloc}/api/outer/ats-apply/website/job",
            json={
                "orgId": org_id,
                "jobId": job_id,
                "siteId": site_id,
                "locale": "zh-CN",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": resolved_site_url},
            timeout=20,
        )
        response.raise_for_status()
        payload = _decode_moka_payload(response.json(), aes_iv)
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            raise _MokaApiVariantUnsupported("job detail data is not an object")
        conflict, observed_identity = _check_identity(
            identity or {"title": title},
            data,
            requested_id=job_id,
            title_normalizer="moka",
        )
        if conflict:
            # Preserve the legacy no-context API's rejection status.
            status = conflict if identity is not None or title else "fetch_failed"
            return _DetailStatus(
                "", status,
                identity_status=conflict.removeprefix("identity_"),
                identity_evidence=observed_identity,
            )
        detail, detail_fields = _moka_detail_content(data)
        if detail:
            status = "complete"
            return _DetailStatus(
                detail, status, detail_url=url,
                attempts=(f"moka_detail_fields:{','.join(detail_fields)}",),
                identity_status="matched" if observed_identity else "request_bound",
                identity_evidence=observed_identity or (f"request_id:{job_id}",),
            )
        primary_status = "official_unavailable" if data else "fetch_failed"
    except requests.Timeout as exc:
        return _api_failure(exc)
    except _MokaApiVariantUnsupported as exc:
        primary_status = "api_variant_unsupported"
        error_type = type(exc).__name__
        error_detail = _safe_exception_detail(exc)
        logger.debug("Moka 岗位详情 API 变体不支持 %s: %s", url, exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Moka 岗位详情 API 获取失败 %s: %s", url, exc)
        return _api_failure(exc)

    attempts.append(f"moka_detail_api:{primary_status}")
    try:
        detail, fallback_status = _fetch_moka_job_from_list(
            resolved_site_url,
            job_id,
            org_id=org_id,
            site_id=site_id,
            aes_iv=aes_iv,
            title=title,
            identity=identity,
        )
        attempts.append(f"moka_list_api:{fallback_status}")
        if detail or fallback_status in _IDENTITY_FAILURES:
            return _DetailStatus(
                detail,
                fallback_status,
                attempts=tuple(attempts),
                error_type=error_type,
                error_detail=error_detail,
            )
        if fallback_status == "api_variant_unsupported":
            primary_status = fallback_status
    except requests.Timeout as exc:
        return _DetailStatus(
            "",
            "timeout",
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
            attempts=(*attempts, "moka_list_api:timeout"),
        )
    except _MokaApiVariantUnsupported as exc:
        primary_status = "api_variant_unsupported"
        error_type = type(exc).__name__
        error_detail = _safe_exception_detail(exc)
        attempts.append("moka_list_api:api_variant_unsupported")
        logger.debug("Moka 岗位列表 API 变体不支持 %s: %s", url, exc)
    except Exception as exc:  # noqa: BLE001
        error_type = type(exc).__name__
        error_detail = _safe_exception_detail(exc)
        attempts.append("moka_list_api:fetch_failed")
        logger.debug("Moka 岗位列表 API 回退失败 %s: %s", url, exc)

    try:
        direct_result = _fetch_moka_direct_page(url, title, identity=identity)
        direct_detail, direct_status = direct_result
        attempts.append(f"moka_direct:{direct_status}")
        if direct_detail or direct_status in _IDENTITY_FAILURES:
            return _DetailStatus(
                direct_detail,
                direct_status,
                attempts=tuple(attempts),
                error_type=error_type,
                error_detail=error_detail,
                detail_url=getattr(direct_result, "detail_url", ""),
            )
    except requests.Timeout as exc:
        return _DetailStatus(
            "",
            "timeout",
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
            attempts=(*attempts, "moka_direct:timeout"),
        )
    except Exception as exc:  # noqa: BLE001
        error_type = type(exc).__name__
        error_detail = _safe_exception_detail(exc)
        attempts.append("moka_direct:fetch_failed")
        logger.debug("Moka 直接详情回退失败 %s: %s", url, exc)
    return _DetailStatus(
        "",
        primary_status,
        attempts=tuple(attempts),
        error_type=error_type,
        error_detail=error_detail,
    )


_HUAWEI_API_ROOT = (
    "https://apigw-dgg-b0.huawei.com/api/apig/channelhw/"
    "recruitmentPosition/pub/"
)
_HUAWEI_API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://career.huawei.com/",
    "User-Agent": "Mozilla/5.0",
    "X-HW-ID": "app_000000035886",
    "X-Jalor-TenantAlias": "hcm",
    "X-Language": "zh_CN",
}


def fetch_huawei_job_description_status(url: str, *, identity: Mapping | None = None) -> tuple[str, str]:
    """Fetch Huawei's detailed position-intention responsibilities."""
    parsed = urlparse(url)
    if parsed.netloc.casefold() != "career.huawei.com":
        return "", "not_applicable"
    match = re.search(r"[?&]advertisementId=(\d+)", url, re.I)
    if not match:
        return "", "fetch_failed"
    try:
        detail_response = requests.post(
            f"{_HUAWEI_API_ROOT}getRecruitmentPositionDetail"
            "?X-HW-ID=app_000000035886",
            json={"advertisementId": match.group(1)},
            headers=_HUAWEI_API_HEADERS,
            timeout=30,
        )
        detail_response.raise_for_status()
        position = detail_response.json().get("data") or {}
        conflict, _ = _check_identity(
            identity or {}, position, requested_id=match.group(1),
            id_fields=("advertisementId",), title_fields=(*_TITLE_FIELDS, "jobCnName"),
        )
        if conflict:
            return "", conflict
        job_id = position.get("jobId")
        if not job_id:
            return "", "official_unavailable"

        intention_response = requests.post(
            f"{_HUAWEI_API_ROOT}getPositionIntentionList"
            "?X-HW-ID=app_000000035886",
            json={"jobId": job_id},
            headers=_HUAWEI_API_HEADERS,
            timeout=30,
        )
        intention_response.raise_for_status()
        intentions = intention_response.json().get("data") or []

        parts = []
        seen = set()
        for item in intentions:
            conflict, _ = _check_identity({}, item, requested_id=str(job_id), id_fields=("jobId",))
            if conflict:
                return "", conflict
            name = _clean_api_text(item.get("positionIntention"))
            duty = _clean_api_text(item.get("jobResponsibilities"))
            requirement = _clean_api_text(item.get("jobDemand"))
            signature = (name, duty, requirement)
            if signature in seen or not (duty or requirement):
                continue
            seen.add(signature)
            if name:
                parts.extend(["岗位方向", name])
            if duty:
                parts.extend(["岗位职责", duty])
            if requirement:
                parts.extend(["任职要求", requirement])

        if not parts:
            duty = _clean_api_text(position.get("mainBusiness"))
            requirement = _clean_api_text(position.get("jobRequire"))
            if duty and "详见岗位意向" not in duty:
                parts.extend(["岗位职责", duty])
            if requirement and "详见岗位意向" not in requirement:
                parts.extend(["任职要求", requirement])
        detail = "\n".join(parts)
        return (detail, "complete") if detail else ("", "official_unavailable")
    except requests.Timeout as exc:
        logger.debug("华为岗位详情 API 超时 %s: %s", url, exc)
        return _api_failure(exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("华为岗位详情 API 获取失败 %s: %s", url, exc)
        return _api_failure(exc)


def fetch_beisen_job_description_status(url: str, *, identity: Mapping | None = None) -> tuple[str, str]:
    """Fetch the detail payload exposed by modern Beisen campus portals."""
    parsed = urlparse(url)
    if not parsed.netloc.casefold().endswith(".zhiye.com"):
        return "", "not_applicable"
    mobile_params = dict(parse_qsl(parsed.fragment.partition("?")[2], keep_blank_values=True))
    is_mobile = parsed.netloc.casefold().endswith(".m.zhiye.com") and bool(mobile_params.get("id"))
    match = re.search(r"[?&]jobAdId=([^&#]+)", url, re.I)
    requested_id = mobile_params["id"] if is_mobile else (match.group(1) if match else "")
    if not requested_id:
        return "", "fetch_failed"
    category_match = re.match(r"^/(\d+)/detail", parsed.path)
    category = mobile_params.get("jc") or (category_match.group(1) if category_match else "2")
    bound_identity = identity or {}
    host_conflict, host_evidence = _beisen_host_binding(url, bound_identity)
    if host_conflict:
        return _DetailStatus(
            "", host_conflict, detail_url=url,
            identity_status=host_conflict.removeprefix("identity_"),
            identity_evidence=host_evidence,
        )
    try:
        api_url = (
            f"{parsed.scheme}://{parsed.netloc}/LightBoltAPI/JobAd/Info"
            if is_mobile
            else f"{parsed.scheme}://{parsed.netloc}/api/JobAd/GetJobAdInfo"
        )
        params = (
            {
                "adid": requested_id,
                "shareid": mobile_params.get("shareid", ""),
                "token": mobile_params.get("token", ""),
                "From": parsed.fragment,
            }
            if is_mobile
            else {
                "jobAdId": requested_id,
                "category": category,
                "displayFields": (
                    '["jobAdName","Duty","Require","Category","Kind",'
                    '"LocId","PostDate"]'
                ),
            }
        )
        response = requests.get(
            api_url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": url},
            timeout=25,
        )
        response.raise_for_status()
        response_url = str(getattr(response, "url", "") or "").strip()
        if response_url:
            request_host = (parsed.hostname or "").casefold().rstrip(".")
            response_host = (urlparse(response_url).hostname or "").casefold().rstrip(".")
            if response_host != request_host:
                return _DetailStatus(
                    "", "identity_mismatch", detail_url=url,
                    identity_status="mismatch",
                    identity_evidence=(
                        *host_evidence,
                        f"response_host:{response_host}",
                        "reason:beisen_redirect_host_mismatch",
                    ),
                )
        content = getattr(response, "content", None)
        if is_mobile and isinstance(content, (bytes, bytearray)):
            payload = json.loads(bytes(content).decode("utf-8-sig"))
        else:
            payload = response.json()
        data = payload.get("Data") or {}
        conflict, identity_evidence = _beisen_identity_check(
            bound_identity,
            data,
            requested_id=requested_id,
            url=url,
            host_evidence=host_evidence,
            request_namespace="job_ad_id" if is_mobile else "request_id",
        )
        if conflict:
            return _DetailStatus(
                "", conflict, detail_url=url,
                identity_status=conflict.removeprefix("identity_"),
                identity_evidence=identity_evidence,
            )
        duty = _clean_api_text(data.get("DutyStr") if is_mobile else data.get("Duty"))
        requirement = _clean_api_text(
            data.get("RequireStr") if is_mobile else data.get("Require")
        )
        parts = []
        if duty:
            parts.extend(["岗位职责", duty])
        if requirement:
            parts.extend(["任职要求", requirement])
        detail = "\n".join(parts)
        if not detail:
            return _DetailStatus(
                "", "official_unavailable" if data else "fetch_failed", detail_url=url,
                identity_status="matched" if _beisen_bound_identity(bound_identity) else "request_bound",
                identity_evidence=identity_evidence,
            )
        status = "complete"
        return _DetailStatus(
            detail,
            status,
            detail_url=url,
            attempts=(("beisen_mobile_api" if is_mobile else "beisen_detail_api") + f":{status}",),
            identity_status="matched" if _beisen_bound_identity(bound_identity) else "request_bound",
            identity_evidence=identity_evidence or (f"request_id:{requested_id}",),
        )
    except requests.Timeout as exc:
        logger.debug("北森岗位详情 API 超时 %s: %s", url, exc)
        return _api_failure(exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("北森岗位详情 API 获取失败 %s: %s", url, exc)
        return _api_failure(exc)


@lru_cache(maxsize=1)
def _configured_careers_urls() -> dict[str, str]:
    path = Path(__file__).with_name("config.yaml")
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(item.get("name") or ""): str(item.get("careers_url") or "")
        for item in config.get("companies") or []
    }


def extract_configured_page_jd(html: str, title: str) -> str:
    """Extract one job's full JD from an official campaign/listing page."""
    return _extract_scoped_jd(html, {"title": title}, is_list=True).detail


def fetch_configured_page_job_description(job: dict) -> str:
    """Legacy string wrapper for identity-aware campaign/list hydration."""
    return fetch_configured_page_job_description_result(job).detail


def _render_configured_page_result(
    job: Mapping, url: str, *, is_list: bool,
) -> JobDetailHydrationResult:
    """Use one bounded browser attempt after the plain list request is inconclusive."""

    try:
        render_options = {
            "timeout_ms": _LIST_RENDER_TIMEOUT_MS,
            "extra_wait_ms": _LIST_RENDER_WAIT_MS,
        }
        entry_click_texts = job.get("entry_click_texts")
        if isinstance(entry_click_texts, (list, tuple)):
            render_options["click_texts"] = [
                str(value).strip() for value in entry_click_texts if str(value).strip()
            ]
        if isinstance(job.get("detail_interaction"), Mapping):
            interaction = job.get("detail_interaction")
            bind_job_id = interaction.get("bind_job_id", True) is not False
            render_options.update(
                detail_interaction=interaction,
                detail_title=str(job.get("title") or ""),
                detail_job_id=(
                    (_requested_id(job) or _url_job_id(url))
                    if bind_job_id
                    else _url_job_id(url)
                ),
            )
        html = render_page(url, **render_options)
    except requests.Timeout as exc:
        return JobDetailHydrationResult(
            "",
            "timeout",
            "configured_page_render",
            url,
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return JobDetailHydrationResult(
            "",
            "fetch_failed",
            "configured_page_render",
            url,
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
        )
    if not html:
        return JobDetailHydrationResult("", "render_failed", "configured_page_render", url)
    access_status = _access_control_status(html)
    if access_status:
        return JobDetailHydrationResult("", access_status, "configured_page_render", url)
    return _extract_scoped_jd(
        html,
        job,
        detail_url=url,
        source="configured_page_render",
        is_list=is_list,
    )


def fetch_configured_page_job_description_result(job: dict) -> JobDetailHydrationResult:
    urls = list(dict.fromkeys(
        str(job.get(name) or "").strip()
        for name in ("careers_url", "campaign_url", "source_url", "list_url", "jd_url", "detail_url")
        if str(job.get(name) or "").startswith(("http://", "https://"))
    ))
    # A current job's provenance is authoritative; legacy config is last-resort only.
    if not urls:
        legacy = _configured_careers_urls().get(str(job.get("company") or ""), "")
        if legacy.startswith(("http://", "https://")):
            urls.append(legacy)
    attempts: list[str] = []
    result = JobDetailHydrationResult("", "list_url", source="configured_page")
    for source_url in urls:
        url = source_url
        visited = set()
        for depth in range(3):
            if url in visited:
                break
            visited.add(url)
            is_list = depth == 0
            resolved_url = url
            request_result: JobDetailHydrationResult
            try:
                response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                response.raise_for_status()
                if getattr(response, "apparent_encoding", None):
                    response.encoding = response.apparent_encoding
                resolved_url = str(getattr(response, "url", "") or url)
                access_status = _access_control_status(response.text)
                if access_status:
                    request_result = JobDetailHydrationResult(
                        "", access_status, "configured_page", resolved_url,
                    )
                    attempts.append(f"configured_page:{request_result.status}")
                    return replace(request_result, attempts=tuple(attempts))
                result = _extract_scoped_jd(
                    response.text, job, detail_url=resolved_url, source="configured_page",
                    is_list=is_list,
                )
                attempts.append(f"configured_page:{result.status}")
                request_result = replace(result, attempts=tuple(attempts))
                if result.complete and not (is_list and job.get("entry_click_texts")):
                    return request_result
                if result.status in _IDENTITY_FAILURES and not is_list:
                    return request_result
                if result.status == "detail_link" and result.detail_url not in visited:
                    url = result.detail_url
                    continue
            except Exception as exc:
                status = "timeout" if isinstance(exc, requests.Timeout) else "fetch_failed"
                attempts.append(f"configured_page:{status}")
                request_result = JobDetailHydrationResult(
                    "",
                    status,
                    "configured_page",
                    url,
                    tuple(attempts),
                    type(exc).__name__,
                    _safe_exception_detail(exc),
                )
                resolved_url = url

            rendered = _render_configured_page_result(
                job, resolved_url, is_list=is_list,
            )
            attempts.append(f"{rendered.source}:{rendered.status}")
            rendered = replace(
                rendered,
                attempts=tuple(attempts),
                error_type=rendered.error_type or request_result.error_type,
            )
            if rendered.complete or rendered.status in _IDENTITY_FAILURES:
                return rendered
            if rendered.status in {"login_required", "captcha_required"}:
                return rendered
            # A bounded render fallback must not erase a more specific request
            # failure (especially a resolved detail timeout) when the browser
            # attempt is unavailable or fails independently.
            if rendered.status in {"fetch_failed", "render_failed"} and request_result.status in {
                "timeout", "fetch_failed",
            }:
                return replace(
                    request_result,
                    attempts=tuple(attempts),
                    error_type=request_result.error_type or rendered.error_type,
                )
            if rendered.status == "detail_link" and rendered.detail_url not in visited:
                url = rendered.detail_url
                continue
            result = rendered
            break
    return replace(result, attempts=tuple(attempts)) if attempts else result


def _adapt_jd_detail_result(
    job: Mapping,
    url: str,
    outcome: object,
) -> JobDetailHydrationResult:
    """Adapt the independent JD.com result to the core hydration contract."""

    status = str(getattr(outcome, "status", "fetch_failed") or "fetch_failed")
    if status in {"publish_id_mismatch", "title_mismatch"}:
        core_status = "identity_mismatch"
    else:
        core_status = status
    identity_evidence = tuple(
        str(item)
        for item in (getattr(outcome, "identity_evidence", ()) or ())
        if str(item)
    )
    observed_id = str(getattr(outcome, "publish_id", "") or "")
    observed_title = str(getattr(outcome, "title", "") or "")
    observed_req_id = str(getattr(outcome, "req_id", "") or "")
    identity_evidence = _merge_identity_evidence(
        ((f"native_id:{observed_id}", f"post_id:{observed_id}") if observed_id else ()),
        ((f"title:{observed_title}",) if observed_title else ()),
        ((f"req_id:{observed_req_id}",) if observed_req_id else ()),
        identity_evidence,
    )
    observed_identity_status = str(getattr(outcome, "identity_status", "") or "")
    if core_status == "identity_mismatch":
        identity_status = "mismatch"
    elif observed_identity_status == "matched":
        identity_status = "matched"
    elif core_status == "complete":
        identity_status = "request_bound"
    else:
        identity_status = observed_identity_status
    detail_url = str(getattr(outcome, "detail_url", "") or url)
    return _detail_result(
        dict(job),
        str(getattr(outcome, "detail", "") or ""),
        core_status,
        source="jd_official_api",
        detail_url=detail_url,
        attempts=(f"jd_official_api:{status}",),
        error_type=str(getattr(outcome, "error_type", "") or ""),
        error_detail=str(getattr(outcome, "error_detail", "") or ""),
        identity_status=identity_status,
        identity_evidence=identity_evidence,
        allow_short=core_status == "complete",
        capture_method="official_api",
        terminal_observed=core_status == "complete",
        remaining_controls=[],
    )


def _adapt_beisen_legacy_result(
    job: Mapping,
    url: str,
    outcome: object,
) -> JobDetailHydrationResult:
    """Adapt the standalone legacy Beisen DOM parser to core hydration."""

    status = str(getattr(outcome, "status", "fetch_failed") or "fetch_failed")
    if status == "identity_mismatch":
        core_status = "identity_mismatch"
    elif status == "complete":
        core_status = "complete"
    elif status in {"identity_unverified", "body_missing", "not_ready"}:
        core_status = "content_incomplete"
    else:
        core_status = status

    observed_identity_status = str(getattr(outcome, "identity_status", "") or "")
    if core_status == "identity_mismatch":
        identity_status = "mismatch"
    elif observed_identity_status in {"matched", "request_bound", "unverified"}:
        identity_status = observed_identity_status
    elif core_status == "complete":
        identity_status = "request_bound"
    else:
        identity_status = ""

    detail_url = str(getattr(outcome, "detail_url", "") or url)
    observed_job_id = str(getattr(outcome, "job_id", "") or "")
    observed_title = str(getattr(outcome, "title", "") or "")
    raw_diagnostics = getattr(outcome, "diagnostics", {})
    diagnostics = raw_diagnostics if isinstance(raw_diagnostics, Mapping) else {}
    observed_job_ids = diagnostics.get("observed_job_ids", ())
    observed_titles = diagnostics.get("observed_titles", ())
    identity_evidence = _merge_identity_evidence(
        tuple(
            str(item)
            for item in (getattr(outcome, "identity_evidence", ()) or ())
            if str(item)
        ),
        ((f"route_id:{observed_job_id}",) if observed_job_id else ()),
        ((f"title:{observed_title}",) if observed_title else ()),
        tuple(
            f"observed_id:{item}"
            for item in observed_job_ids
            if str(item)
        ) if isinstance(observed_job_ids, (list, tuple, set)) else (),
        tuple(
            f"title:{item}"
            for item in observed_titles
            if str(item)
        ) if isinstance(observed_titles, (list, tuple, set)) else (),
    )
    return _detail_result(
        dict(job),
        str(getattr(outcome, "body", "") or getattr(outcome, "detail", "") or ""),
        core_status,
        source="beisen_legacy_detail",
        detail_url=detail_url,
        attempts=(f"beisen_legacy_detail:{status}",),
        identity_status=identity_status,
        identity_evidence=identity_evidence,
        allow_short=core_status == "complete",
        capture_method="detail_dom",
        terminal_observed=core_status == "complete",
        remaining_controls=[],
    )


def _tencent_observed_identity_evidence(observed: Mapping) -> tuple[str, ...]:
    """Keep Tencent's public post ID and internal ID visible as separate evidence."""

    evidence: list[str] = []
    for field in _TENCENT_POST_ID_FIELDS:
        value = observed.get(field)
        if value not in (None, ""):
            evidence.append(f"post_id:{value}")
    for field in _TENCENT_INTERNAL_ID_FIELDS:
        value = observed.get(field)
        if value not in (None, ""):
            evidence.append(f"internal_job_id:{value}")
    for field in _TENCENT_TITLE_FIELDS:
        value = observed.get(field)
        if isinstance(value, Mapping):
            value = value.get("name")
        if value not in (None, ""):
            evidence.append(f"title:{value}")
    return tuple(dict.fromkeys(evidence))[:_DIAGNOSTIC_ITEMS_LIMIT]


def fetch_tencent_job_description_status(url: str, *, identity: Mapping | None = None) -> tuple[str, str]:
    """Fetch regular and Qingyun-topic JD fields from Tencent's detail API."""
    parsed = urlparse(url)
    if parsed.netloc.casefold() != "join.qq.com":
        return "", "not_applicable"
    match = re.search(r"(?:[?&]postId=)(\d+)", url, re.I)
    if not match:
        return "", "fetch_failed"
    for attempt in range(3):
        try:
            response = requests.get(
                "https://join.qq.com/api/v1/jobDetails/getJobDetailsByPostId",
                params={"timestamp": int(time.time() * 1000), "postId": match.group(1)},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://join.qq.com/post.html",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                return "", "fetch_failed"
            if payload.get("status") == 404 or "下架" in str(payload.get("message") or ""):
                return "", "job_offline"
            data = payload.get("data") or {}
            if not isinstance(data, Mapping):
                return "", "official_unavailable" if payload.get("code") == 0 else "fetch_failed"
            conflict, generic_identity_evidence = _check_identity(
                identity or {},
                data,
                requested_id=match.group(1),
                id_fields=_TENCENT_POST_ID_FIELDS,
                title_fields=_TENCENT_TITLE_FIELDS,
            )
            identity_evidence = _merge_identity_evidence(
                generic_identity_evidence,
                _tencent_observed_identity_evidence(data),
            )
            if conflict:
                return _DetailStatus(
                    "",
                    conflict,
                    detail_url=url,
                    identity_status=conflict.removeprefix("identity_"),
                    identity_evidence=identity_evidence,
                )
            duties = _clean_api_text(
                data.get("desc")
                or data.get("topicDetail")
                or data.get("introduction")
            )
            requirements = _clean_api_text(
                data.get("request")
                or data.get("topicRequirement")
            )
            parts = []
            if duties:
                parts.extend(["岗位职责", duties])
            if requirements:
                parts.extend(["任职要求", requirements])
            detail = "\n".join(parts)
            if detail:
                return _DetailStatus(
                    detail,
                    "complete",
                    detail_url=url,
                    identity_status="matched" if generic_identity_evidence else "request_bound",
                    identity_evidence=identity_evidence,
                )
            status = "official_unavailable" if payload.get("code") == 0 else "fetch_failed"
            return _DetailStatus(
                "",
                status,
                detail_url=url,
                identity_status="matched" if generic_identity_evidence else "request_bound",
                identity_evidence=identity_evidence,
            )
        except requests.Timeout as exc:
            logger.debug("腾讯岗位详情 API 超时 %s: %s", url, exc)
            return _api_failure(exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "腾讯岗位详情 API 获取失败 %s（%d/3）: %s",
                url,
                attempt + 1,
                exc,
            )
            if attempt < 2:
                time.sleep(attempt + 1)
            else:
                return _api_failure(exc)
    return "", "fetch_failed"


_TITLE_SELECTOR = (
    "h1, h2, h3, h4, .job-title, .job-name, .post_name, .position-title, .position-name, "
    ".ant-drawer-title, .ant-modal-title, [itemprop='title'], [data-job-title]"
)
_CARD_SELECTOR = (
    "article, li, .job-card, .job-item, .position-card, .ant-card, .ant-drawer-content, "
    ".ant-modal-content, [data-job-id], [data-position-id]"
)
_NON_JOB_HEADINGS = {
    _identity_key(value) for value in (
        *_START_MARKERS, *_END_MARKERS, "岗位要求", "任职要求", "任职资格", "招聘要求",
        "工作地点", "招聘职位", "职位列表", "校园招聘", "社会招聘", "招聘岗位",
        "岗位详情", "职位详情", "Jobs", "Careers", "Requirements", "Qualifications",
    )
}


def _scope_identity(scope, title: str, detail_url: str) -> dict:
    observed = {"title": title}
    for node in [scope, *scope.select("[data-job-id], [data-position-id], [data-company], [itemprop='hiringOrganization']")]:
        for attr, field in (("data-job-id", "id"), ("data-position-id", "positionId"), ("data-company", "company")):
            if node.get(attr):
                observed[field] = node[attr]
        if node.get("itemprop") == "hiringOrganization":
            observed["companyName"] = node.get_text(" ", strip=True)
    # A title link supplies native identity even when the card has no data attributes.
    links = ([scope] if scope.name == "a" else []) + scope.select("a[href]")
    ids = {_url_job_id(urljoin(detail_url, link.get("href", ""))) for link in links}
    ids.discard("")
    if len(ids) == 1:
        observed["job_id"] = ids.pop()
    return observed


def _extract_moka_rendered_detail(soup, job: Mapping, detail_url: str, source: str) -> JobDetailHydrationResult | None:
    parsed = urlsplit(detail_url)
    if not _moka_job_id(detail_url) or not (
        (parsed.hostname or "").endswith(".mokahr.com")
        or re.search(r"/(?:campus_apply|campus-recruitment)/", parsed.path)
    ):
        return None
    title_selector = (
        '[class*="job-info-"] [class^="title-"], '
        '[class*="header-wrapper-"] [class^="title-"]'
    )
    headings = soup.select(title_selector)
    if len(headings) != 1:
        return None
    heading = headings[0]
    scope = next((parent for parent in heading.parents if any(
        str(name).startswith(("job-details-", "left-panel-"))
        for name in parent.get("class", [])
    ) and parent.select('[class*="job-description-"]')), None)
    if scope is None:
        return None
    name = heading.get_text(" ", strip=True)
    conflict, evidence = _check_identity(
        job,
        _scope_identity(scope, name, detail_url),
        requested_id=_url_job_id(detail_url),
        title_normalizer="moka",
    )
    if conflict:
        return _detail_result(dict(job), "", conflict, source=source, detail_url=detail_url, identity_evidence=evidence)
    for node in scope.select("[data-job-id], [data-position-id]"):
        conflict, node_evidence = _check_identity(
            job,
            _scope_identity(node, name, detail_url),
            requested_id=_url_job_id(detail_url),
            title_normalizer="moka",
        )
        if conflict:
            return _detail_result(dict(job), "", conflict, source=source, detail_url=detail_url, identity_evidence=node_evidence)
    # Desktop/mobile description copies are acceptable only when identical.
    descriptions = {}
    for node in scope.select('[class*="job-description-"]'):
        text = node.get_text("\n", strip=True)
        if text:
            descriptions.setdefault(" ".join(text.split()), text)
    if len(descriptions) > 1:
        return _detail_result(dict(job), "", "identity_ambiguous", source=source, detail_url=detail_url)
    remaining_controls = _scope_remaining_controls(scope)
    capture_complete = not remaining_controls
    return _detail_result(
        dict(job), next(iter(descriptions.values()), ""), "content_incomplete",
        source=source, detail_url=detail_url, identity_status="matched", identity_evidence=evidence,
        allow_short=bool(descriptions) and capture_complete, capture_method="detail_dom",
        terminal_observed=bool(descriptions) and capture_complete,
        remaining_controls=remaining_controls,
    )


def _extract_scoped_jd(
    html: str, job: Mapping, *, detail_url: str = "", source: str = "render", is_list: bool = False,
) -> JobDetailHydrationResult:
    soup = BeautifulSoup(html or "", "html.parser")
    capture_meta = _capture_metadata(html)
    captured_final_url = str(capture_meta.get("final_url") or "").strip()
    if captured_final_url.startswith(("http://", "https://")):
        detail_url = captured_final_url
    scripts = list(soup.find_all("script"))
    script_rows = []
    pattern = re.compile(r"name\s*:\s*'((?:\\.|[^'])*)'\s*,\s*value\s*:\s*'((?:\\.|[^'])*)'", re.S)
    for script in scripts:
        for name, value in pattern.findall(script.string or script.get_text()):
            script_rows.append((name.replace("\\'", "'"), value.replace("\\'", "'").replace("\\n", "\n")))
    for tag in soup.select("script, style, noscript, svg, head, nav, footer, [hidden], [aria-hidden='true']"):
        if getattr(tag, "attrs", None) is not None:
            tag.decompose()
    for tag in soup.select("[style]"):
        attributes = getattr(tag, "attrs", None)
        if not isinstance(attributes, Mapping):
            continue
        if re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
            str(attributes.get("style") or ""),
            re.I,
        ):
            tag.decompose()

    # Hotjob repeats the detail heading in a sticky apply bar; category badges
    # are siblings of the title text, not part of the job's identity.
    host = urlsplit(detail_url).hostname or ""
    title_normalizer = (
        "moka"
        if source.startswith("moka")
        or (host.casefold() == "mokahr.com" or host.casefold().endswith(".mokahr.com"))
        or re.search(r"/(?:campus_apply|campus-recruitment)/", urlsplit(detail_url).path, re.I)
        else ""
    )
    if not is_list and host.endswith(".hotjob.cn") and _url_job_id(detail_url):
        detail_titles = soup.select(".pos-detail-hd__titBar .tit")
        if len(detail_titles) == 1:
            heading = detail_titles[0]
            name = " ".join(str(text).strip() for text in heading.find_all(string=True, recursive=False)).strip()
            if name:
                heading["data-job-title"] = name
                for bar in soup.select(".fixed-postInfo"):
                    sticky_title = bar.select_one(".tit")
                    if sticky_title and _identity_key(sticky_title.get_text(" ", strip=True)) == _identity_key(name):
                        conflict, _ = _check_identity(job, _scope_identity(bar, name, detail_url), requested_id=_url_job_id(detail_url))
                        if not conflict:
                            bar.decompose()

    load_state = str(capture_meta.get("load_state") or "").casefold()
    if load_state == "unknown":
        load_state = ""

    # A verified browser interaction marks the exact drawer/card that supplied
    # the detail. Parse that bounded container instead of re-discovering an
    # identity from every job title still visible behind it on the list page.
    if (
        capture_meta.get("status") == "complete"
        and str(capture_meta.get("method") or "").startswith("detail_interaction:")
    ):
        captured_scopes = soup.select("[data-recruitops-detail-container='true']")
        if len(captured_scopes) == 1:
            soup = BeautifulSoup(str(captured_scopes[0]), "html.parser")

    def outcome(status: str, *, evidence: tuple[str, ...] = ()) -> JobDetailHydrationResult:
        identity_status = status.removeprefix("identity_") if status in _IDENTITY_FAILURES else ""
        if status in _IDENTITY_FAILURES:
            return _detail_result(
                dict(job), "", status,
                source=source,
                detail_url=detail_url,
                attempts=(f"{source}:{status}",),
                identity_status=identity_status,
                identity_evidence=evidence,
                capture_method=str(capture_meta.get("method") or ""),
                terminal_observed=bool(capture_meta.get("terminal_observed")),
                remaining_controls=capture_meta.get("remaining_controls"),
                load_state=load_state,
            )
        return JobDetailHydrationResult(
            "", status, source, detail_url, (f"{source}:{status}",),
            identity_status=identity_status,
            identity_evidence=evidence,
            capture_evidence=_capture_evidence(
                detail="",
                status=status,
                source=source,
                detail_url=detail_url,
                identity_status=identity_status,
                method=str(capture_meta.get("method") or ""),
                terminal_observed=bool(capture_meta.get("terminal_observed")),
                remaining_controls=capture_meta.get("remaining_controls"),
                load_state=load_state,
            ),
        )

    load_state_status = {
        "login_required": "login_required",
        "not_found": "job_offline",
        "request_failed": "render_failed",
        "timeout": "timeout",
    }.get(load_state)
    if load_state_status:
        return outcome(load_state_status)

    route_id = _url_job_id(detail_url)
    if not is_list and _requested_id(job) and route_id and _identity_key(_requested_id(job)) != _identity_key(route_id):
        return outcome("identity_mismatch", evidence=(f"native_id:{route_id}",))
    if not is_list:
        moka_detail = _extract_moka_rendered_detail(soup, job, detail_url, source)
        if moka_detail is not None:
            return moka_detail
    title = str(job.get("title") or "")
    nodes = []
    for node in soup.select(_TITLE_SELECTOR):
        name = node.get("data-job-title") or node.get_text(" ", strip=True)
        key = _identity_key(name).strip(":：")
        if not key or key in _NON_JOB_HEADINGS or len(name) > 150 or _is_error_page_heading(name):
            continue
        if any(parent is previous for parent in node.parents for previous in nodes):
            continue
        nodes.append(node)
    for node in soup.select("a[href]"):
        if not _url_job_id(urljoin(detail_url, node.get("href", ""))):
            continue
        if any(node is previous or node in list(previous.parents) or previous in list(node.parents) for previous in nodes):
            continue
        if node.get_text(" ", strip=True) and not _is_error_page_heading(node.get_text(" ", strip=True)):
            nodes.append(node)
    title_key = _moka_title_identity_key if title_normalizer == "moka" else _identity_key
    if title and not any(title_key(node.get_text(" ", strip=True)) == title_key(title) for node in nodes):
        for text_node in soup.find_all(string=True):
            if title_key(text_node) == title_key(title):
                if not any(previous is text_node.parent or previous in list(text_node.parents) for previous in nodes):
                    nodes.append(text_node.parent)
    if not nodes:
        nodes = soup.select("[data-job-id], [data-position-id]")

    candidates = []
    conflicts = []
    for node in nodes:
        scope = node
        while scope.parent is not None:
            if any(other is not node and (other is scope.parent or any(parent is scope.parent for parent in other.parents)) for other in nodes):
                break
            if scope in soup.select(_CARD_SELECTOR):
                break
            scope = scope.parent
        name = node.get("data-job-title") or (node.get_text(" ", strip=True) if node in soup.select(_TITLE_SELECTOR + ", a") else "")
        observed = _scope_identity(scope, name, detail_url)
        status, evidence = _check_identity(
            job,
            observed,
            requested_id="" if is_list else route_id,
            title_normalizer=title_normalizer,
        )
        evidence = _merge_identity_evidence(
            evidence,
            _observed_identity_evidence(observed),
        )
        if status:
            conflicts.append(evidence)
            continue
        matches = any(item.startswith(("title:", "native_id:")) for item in evidence)
        if matches or (not title and not _requested_id(job) and len(nodes) == 1):
            candidates.append((scope, evidence))

    if len(candidates) > 1:
        return outcome(
            "identity_ambiguous",
            evidence=_merge_identity_evidence(*(item[1] for item in candidates)),
        )
    if not candidates and nodes:
        status = "identity_ambiguous" if len(nodes) > 1 else "identity_mismatch"
        return outcome(status, evidence=_merge_identity_evidence(*conflicts))
    if not candidates and script_rows:
        matching = [(name, value) for name, value in script_rows if _identity_key(name) == _identity_key(title)]
        if len(matching) != 1:
            return outcome(
                "identity_ambiguous" if len(script_rows) > 1 else "identity_mismatch",
                evidence=tuple(
                    f"title:{_bounded_diagnostic_text(name)}"
                    for name, _ in script_rows[:_DIAGNOSTIC_ITEMS_LIMIT]
                ),
            )
        name, value = matching[0]
        candidates = [(BeautifulSoup(value, "html.parser"), (f"title:{name}",))]
    if candidates:
        scope, evidence = candidates[0]
    elif is_list:
        return outcome("list_url")
    else:
        scope, evidence = soup.find("main") or soup.body or soup, ()
    scoped_html = str(scope)
    scope_controls = _scope_remaining_controls(scope)
    metadata_controls = capture_meta.get("remaining_controls")
    remaining_controls = list(dict.fromkeys(
        [
            *(
                metadata_controls
                if isinstance(metadata_controls, list)
                else []
            ),
            *scope_controls,
        ]
    ))
    detail = _extract_detail_text(scoped_html, title)
    interaction_capture_verified = bool(
        capture_meta.get("status") == "complete"
        and str(capture_meta.get("method") or "").startswith("detail_interaction:")
        and capture_meta.get("terminal_observed")
        and (not load_state or load_state == "ready")
        and not remaining_controls
    )
    if interaction_capture_verified:
        interaction_detail = _extract_verified_interaction_text(scoped_html, title)
        if interaction_detail:
            detail = interaction_detail
    metadata_capture_verified = bool(
        capture_meta.get("status") == "complete"
        and capture_meta.get("method")
        and capture_meta.get("terminal_observed")
        and (not load_state or load_state == "ready")
        and not remaining_controls
    )
    if not detail:
        text = scope.get_text("\n", strip=True)
        if _has_structured_untitled_jd(text, text):
            detail = text
    identity_status = "matched" if evidence else "request_bound" if route_id else "unverified"
    interaction_requested = isinstance(job.get("detail_interaction"), Mapping)
    bounded_dom = bool(
        detail
        and identity_status in {"matched", "request_bound"}
        and bool(evidence)
        and (
            (
                not interaction_requested
                and not is_list
                and bool(detail_url)
                and source in {"render", "configured_page", "configured_page_render", "moka_direct"}
                and (not load_state or load_state == "ready")
            )
            or metadata_capture_verified
        )
        and not remaining_controls
    )
    result = _detail_result(
        dict(job), detail, "content_incomplete", source=source, detail_url=detail_url,
        identity_status=identity_status,
        identity_evidence=evidence or ((f"request_id:{route_id}",) if route_id else ()),
        allow_short=bounded_dom,
        capture_method=str(capture_meta.get("method") or ""),
        terminal_observed=(bool(capture_meta.get("terminal_observed")) and not remaining_controls) or bounded_dom,
        remaining_controls=remaining_controls,
        load_state=load_state,
    )
    if is_list and not bounded_dom and result.detail:
        result = replace(
            result,
            detail="",
            status="content_incomplete",
            capture_evidence=_capture_evidence(
                detail="",
                status="content_incomplete",
                source=source,
                detail_url=detail_url,
                identity_status=identity_status,
                method=str(capture_meta.get("method") or ""),
                terminal_observed=bool(capture_meta.get("terminal_observed")) and not remaining_controls,
                remaining_controls=remaining_controls,
                load_state=load_state,
            ),
        )
    if result.complete:
        return result
    if len(nodes) > 1 and candidates and not detail and not scope.select("a[href]") and scope.name != "a":
        return outcome(
            "identity_ambiguous",
            evidence=_merge_identity_evidence(*(item[1] for item in candidates), *conflicts),
        )
    if candidates:
        links = ([scope] if scope.name == "a" else []) + scope.select("a[href]")
        urls = set()
        for link in links:
            target = urljoin(detail_url, link.get("href", ""))
            if target.startswith(("http://", "https://")) and target != detail_url and _url_job_id(target):
                native_id = _requested_id(job)
                if not native_id or _identity_key(native_id) == _identity_key(_url_job_id(target)):
                    urls.add(target)
        if len(urls) > 1:
            return outcome(
                "identity_ambiguous",
                evidence=tuple(
                    f"route_id:{_bounded_diagnostic_text(_url_job_id(target))}"
                    for target in sorted(urls)
                    if _url_job_id(target)
                )[:_DIAGNOSTIC_ITEMS_LIMIT],
            )
        if urls:
            return replace(result, detail="", status="detail_link", detail_url=urls.pop())
    return replace(result, detail="")


def extract_rendered_jd(html: str, title: str = "") -> str:
    """Legacy string extraction, with the same identity guard as hydration."""
    return _extract_scoped_jd(html, {"title": title}).detail


def _extract_detail_text(html: str, title: str = "") -> str:
    """Extract section text only after its single-job scope has been selected."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    lines = [line for line in lines if line]

    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    lines = deduped

    start = next(
        (index for index, line in enumerate(lines)
         if any(line.casefold() == marker.casefold() for marker in _START_MARKERS)),
        None,
    )
    if start is None:
        start = next(
            (index for index, line in enumerate(lines)
             if any(marker.casefold() in line.casefold() for marker in _START_MARKERS)),
            None,
        )
    if start is None:
        return ""

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if any(lines[index].casefold() == marker.casefold() for marker in _END_MARKERS):
            end = index
            break

    detail_lines = lines[start:end]
    # Some SPA pages keep both desktop and mobile detail components in the DOM.
    # The heading is shared while the complete content block appears twice.
    if len(detail_lines) >= 5:
        heading, content = detail_lines[0], detail_lines[1:]
        if len(content) % 2 == 0:
            midpoint = len(content) // 2
            if content[:midpoint] == content[midpoint:]:
                detail_lines = [heading, *content[:midpoint]]

    detail = "\n".join(detail_lines).strip()
    if title and detail == title:
        return ""
    return detail


def _extract_verified_interaction_text(html: str, title: str = "") -> str:
    """Read only a verified interaction container, excluding its title and controls."""

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.select(
        "script, style, noscript, svg, button, input, select, textarea, "
        "[role='button'], [role='tab'], [aria-hidden='true'], [hidden]"
    ):
        tag.decompose()
    title_key = _identity_key(title)
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    lines = [
        line for line in lines
        if line and (not title_key or _identity_key(line) != title_key)
    ]
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return "\n".join(deduped).strip()


def _detail_result(
    job: dict,
    detail: str,
    status: str,
    *,
    source: str,
    detail_url: str,
    attempts: tuple[str, ...] = (),
    error_type: str = "",
    error_detail: str = "",
    identity_status: str = "",
    identity_evidence: tuple[str, ...] = (),
    identity_diagnostic: Mapping | None = None,
    allow_short: bool = False,
    capture_method: str = "",
    terminal_observed: bool = False,
    remaining_controls: object = None,
    load_state: str = "",
) -> JobDetailHydrationResult:
    normalized_status = status
    error_detail = _redact_diagnostic_text(error_detail)
    verified_capture = False
    if status in _IDENTITY_FAILURES:
        detail = ""
        identity_status = status.removeprefix("identity_")
    elif detail:
        verified_capture = allow_short and identity_status in {"matched", "request_bound"}
        if verified_capture:
            normalized_status = "complete"
        elif status in {"unknown", "capture_unknown"}:
            normalized_status = "unknown"
        else:
            normalized_status = "content_incomplete"
    final_attempts = attempts or (f"{source}:{normalized_status}",)
    diagnostic = dict(identity_diagnostic or {})
    if normalized_status in _IDENTITY_FAILURES and not diagnostic:
        diagnostic = _identity_diagnostic(
            job,
            normalized_status,
            source=source,
            detail_url=detail_url,
            attempts=final_attempts,
            identity_evidence=identity_evidence,
            error_type=error_type,
            error_detail=error_detail,
        )
    evidence = _capture_evidence(
        detail=detail,
        status=normalized_status,
        source=source,
        detail_url=detail_url,
        identity_status=identity_status,
        method=capture_method,
        terminal_observed=terminal_observed or (verified_capture and bool(detail)),
        remaining_controls=remaining_controls,
        identity_diagnostic=diagnostic,
        load_state=load_state,
    )
    return JobDetailHydrationResult(
        detail=detail,
        status=normalized_status,
        source=source,
        detail_url=detail_url,
        attempts=final_attempts,
        error_type=error_type,
        error_detail=error_detail,
        identity_status=identity_status,
        identity_evidence=identity_evidence,
        identity_diagnostic=diagnostic,
        capture_evidence=evidence,
    )


def fetch_full_job_description_result(job: dict) -> JobDetailHydrationResult:
    """Hydrate one JD and return a non-throwing, diagnosable outcome."""

    url = str(job.get("jd_url") or job.get("detail_url") or "").strip()
    attempts: list[str] = []
    source = ""
    fallback_error_type = ""
    fallback_error_detail = ""

    def api_result(outcome: tuple[str, str], api_source: str) -> JobDetailHydrationResult:
        detail, status = outcome
        api_attempts = getattr(outcome, "attempts", ()) or (f"{api_source}:{status}",)
        observed_identity_status = getattr(outcome, "identity_status", "")
        if status in _IDENTITY_FAILURES:
            result_identity_status = status.removeprefix("identity_")
        elif observed_identity_status:
            result_identity_status = observed_identity_status
        elif status in {"complete", "official_sparse"}:
            result_identity_status = "request_bound"
        else:
            result_identity_status = ""
        observed_identity_evidence = getattr(outcome, "identity_evidence", ())
        if observed_identity_evidence:
            result_identity_evidence = observed_identity_evidence
        elif status in {"complete", "official_sparse"} and _url_job_id(url):
            result_identity_evidence = (f"request_id:{_url_job_id(url)}",)
        else:
            result_identity_evidence = ()
        return _detail_result(
            job, detail, status, source=api_source,
            detail_url=getattr(outcome, "detail_url", "") or url,
            attempts=(*attempts, *api_attempts),
            error_type=getattr(outcome, "error_type", ""),
            error_detail=getattr(outcome, "error_detail", ""),
            identity_status=result_identity_status,
            identity_evidence=result_identity_evidence,
            allow_short=status in {"complete", "official_sparse"},
            capture_method="official_api",
            terminal_observed=status in {"complete", "official_sparse"},
            remaining_controls=[],
        )
    # New database rows always carry an evidence-based cohort status. Keep
    # backwards compatibility for standalone parser tests and legacy callers
    # that have no cohort fields yet.
    try:
        if "cohort_status" in job:
            from . import job_cohorts

            # Title-first admission may discover the official cohort only in
            # the detail. Preserve unknown evidence instead of inventing it.
            title_first_unknown = (
                job.get("detail_capture_policy") == "title_first_v2"
                and str(job.get("cohort_status") or "").lower()
                in {"", "unknown", "unconfirmed"}
                and str(job.get("cohort") or "") in {"", "0", "2027"}
            )
            if not title_first_unknown and not job_cohorts.is_confirmed_current(job):
                return JobDetailHydrationResult("", "cohort_ineligible", detail_url=url)
        if assess_jd_capture(job).complete:
            return JobDetailHydrationResult(
                detail=str(job.get("jd_raw") or "").strip(),
                status="complete",
                source="stored",
                detail_url=url,
                capture_evidence=dict(job.get("capture_evidence") or {}),
            )
        if job.get("link_kind") == "list" or (not url and any(job.get(name) for name in ("careers_url", "campaign_url", "source_url", "list_url"))):
            return fetch_configured_page_job_description_result(job)
        if not url:
            return JobDetailHydrationResult("", "no_detail_url")
        if not url.startswith(("http://", "https://")):
            return JobDetailHydrationResult("", "no_detail_url", detail_url=url)
        route_id = _url_job_id(url)
        host = urlparse(url).netloc.casefold()
        if (
            host != _LENOVO_HOST
            and _requested_id(job)
            and route_id
            and _identity_key(_requested_id(job)) != _identity_key(route_id)
        ):
            return _detail_result(job, "", "identity_mismatch", source="request", detail_url=url)

        if host == "hotjob.cn" or host.endswith(".hotjob.cn"):
            source = "hotjob_api"
            detail, resolved_url = fetch_hotjob_position_detail(url)
            attempts.append("hotjob_api:complete" if detail else "hotjob_api:fetch_failed")
            if resolved_url:
                url = resolved_url
            if detail:
                return _detail_result(
                    job,
                    detail,
                    "complete",
                    source="hotjob_api",
                    detail_url=url,
                    attempts=tuple(attempts),
                    identity_status="request_bound" if _url_job_id(url) else "",
                    identity_evidence=((f"request_id:{_url_job_id(url)}",) if _url_job_id(url) else ()),
                    allow_short=True,
                    capture_method="official_api",
                    terminal_observed=True,
                    remaining_controls=[],
                )

        if host == _LENOVO_HOST:
            source = "lenovo_official_api"
            # Lenovo's detail route is a public SPA shell.  Return its API
            # outcome directly so an unbound or blocked API response cannot
            # fall through to a generic renderer or list-page crawl.
            return api_result(
                fetch_lenovo_job_description_status(url, identity=job),
                source,
            )

        if host == "campus.jd.com":
            source = "jd_official_api"
            from .jd_detail_adapter import fetch_jd_detail

            expected_publish_id = (
                job.get("source_post_id")
                or job.get("source_job_id")
                or _requested_id(job)
                or None
            )
            jd_result = fetch_jd_detail(
                url,
                expected_publish_id=expected_publish_id,
                expected_title=str(job.get("title") or "") or None,
            )
            return _adapt_jd_detail_result(job, url, jd_result)

        moka_site_url, custom_moka = _moka_provenance_site(job, url)
        official_moka_job = (
            host == "mokahr.com"
            or host.endswith(".mokahr.com")
        ) and bool(_moka_job_id(url))
        if moka_site_url or official_moka_job:
            source = "moka_provenance" if custom_moka else "moka_official"
            result = api_result(
                fetch_moka_job_description_status(
                    url,
                    trusted_custom_host=custom_moka,
                    site_url=moka_site_url,
                    title=str(job.get("title") or ""),
                    identity=job,
                ),
                source,
            )
            if result.detail or result.status in _IDENTITY_FAILURES | {"timeout"}:
                return result
            attempts.extend(result.attempts)
            fallback_error_type = result.error_type or fallback_error_type
            fallback_error_detail = result.error_detail or fallback_error_detail

        if host == "career.huawei.com":
            source = "huawei_api"
            return api_result(fetch_huawei_job_description_status(url, identity=job), source)
        if host.endswith(".zhiye.com"):
            from .beisen_legacy_detail import (
                is_beisen_legacy_detail_url,
                parse_beisen_legacy_detail,
            )

            if is_beisen_legacy_detail_url(url):
                source = "beisen_legacy_detail"
                response = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=25,
                )
                response.raise_for_status()
                response_url = str(getattr(response, "url", "") or url)
                expected_job_id = (
                    _requested_id(job)
                    or _diagnostic_route_job_id(response_url)
                    or _diagnostic_route_job_id(url)
                )
                legacy_result = parse_beisen_legacy_detail(
                    str(getattr(response, "text", "") or ""),
                    url=response_url,
                    expected_title=str(job.get("title") or ""),
                    expected_job_id=expected_job_id,
                )
                if str(getattr(legacy_result, "status", "") or "") != "not_applicable":
                    return _adapt_beisen_legacy_result(job, url, legacy_result)
            source = "beisen_api"
            result = api_result(fetch_beisen_job_description_status(url, identity=job), source)
            if result.detail or result.status in _IDENTITY_FAILURES | {"timeout"}:
                return result
            attempts.extend(result.attempts)
            fallback_error_type = result.error_type or fallback_error_type
            fallback_error_detail = result.error_detail or fallback_error_detail
        if host in {"jobs.51job.com", "xyz.51job.com", "xym.51job.com"}:
            result = fetch_configured_page_job_description_result(job)
            if result.complete or result.status in _IDENTITY_FAILURES:
                return replace(result, attempts=(*attempts, *result.attempts))
            attempts.extend(result.attempts)

        if host == "join.qq.com":
            source = "tencent_api"
            return api_result(fetch_tencent_job_description_status(url, identity=job), source)

        source = "feishu_api"
        result = api_result(fetch_feishu_job_description_status(url, identity=job), source)
        if result.status != "not_applicable":
            if result.detail or result.status in _IDENTITY_FAILURES | {"timeout"}:
                return result
            attempts.extend(result.attempts)
            fallback_error_type = result.error_type or fallback_error_type
            fallback_error_detail = result.error_detail or fallback_error_detail

        source = "render"
        # Leave parsing/cleanup time inside the isolated detail worker's deadline.
        render_options = {
            "timeout_ms": 30000,
            "extra_wait_ms": 1500,
            "wait_until": "domcontentloaded",
        }
        if isinstance(job.get("detail_interaction"), Mapping):
            render_options.update(
                detail_interaction=job.get("detail_interaction"),
                detail_title=str(job.get("title") or ""),
                detail_job_id=_requested_id(job) or _url_job_id(url),
            )
        html = render_page(url, **render_options)
        capture_meta = _capture_metadata(html)
        captured_final_url = str(capture_meta.get("final_url") or "").strip()
        render_detail_url = captured_final_url or url
        render_load_state = str(capture_meta.get("load_state") or "").casefold()
        if render_load_state == "unknown":
            render_load_state = ""
        render_state_status = {
            "login_required": "login_required",
            "not_found": "job_offline",
            "request_failed": "render_failed",
            "timeout": "timeout",
        }.get(render_load_state)
        if not html:
            status = render_state_status or "render_failed"
            return _detail_result(
                job,
                "",
                status,
                source="render",
                detail_url=render_detail_url,
                attempts=(*attempts, f"render:{status}"),
                error_type=fallback_error_type,
                error_detail=fallback_error_detail,
                capture_method=str(capture_meta.get("method") or ""),
                terminal_observed=bool(capture_meta.get("terminal_observed")),
                remaining_controls=capture_meta.get("remaining_controls"),
                load_state=render_load_state,
            )
        access_status = _access_control_status(html)
        if access_status:
            status = render_state_status or access_status
            return _detail_result(
                job,
                "",
                status,
                source="render",
                detail_url=render_detail_url,
                attempts=(*attempts, f"render:{status}"),
                error_type=fallback_error_type,
                error_detail=fallback_error_detail,
                capture_method=str(capture_meta.get("method") or ""),
                terminal_observed=bool(capture_meta.get("terminal_observed")),
                remaining_controls=capture_meta.get("remaining_controls"),
                load_state=render_load_state,
            )
        result = _extract_scoped_jd(html, job, detail_url=url)
        if result.status == "detail_link":
            resolved = fetch_configured_page_job_description_result({**job, "careers_url": url})
            return replace(
                resolved,
                attempts=(*attempts, *result.attempts, *resolved.attempts),
                error_type=resolved.error_type or fallback_error_type,
                error_detail=resolved.error_detail or fallback_error_detail,
            )
        return replace(
            result,
            attempts=(*attempts, *result.attempts),
            error_type=result.error_type or fallback_error_type,
            error_detail=result.error_detail or fallback_error_detail,
        )
    except requests.Timeout as exc:
        logger.warning("岗位详情自动补全超时 %s: %s", url, exc)
        return JobDetailHydrationResult(
            "",
            "timeout",
            source=source,
            detail_url=url,
            attempts=(*attempts, f"{source}:timeout"),
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
        )
    except Exception as exc:  # one detail page must not stop a batch
        logger.warning("岗位详情自动补全失败 %s: %s", url, exc)
        return JobDetailHydrationResult(
            "",
            "fetch_failed",
            source=source,
            detail_url=url,
            attempts=(*attempts, f"{source}:fetch_failed"),
            error_type=type(exc).__name__,
            error_detail=_safe_exception_detail(exc),
        )


def fetch_full_job_description(job: dict) -> str:
    """Return the hydrated JD string while preserving the legacy contract."""

    return fetch_full_job_description_result(job).detail
