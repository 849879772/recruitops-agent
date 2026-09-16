"""hotjob.cn（北森 wecruit 系）通用校招爬虫基类。

师兄清单里 ~20 家用 hotjob，URL 形如 https://<sub>.hotjob.cn/SU<id>/pb/account.html。
hotjob 站点把社招/校招分页：
    /pb/social.html  社会招聘  ←  不要（社招陷阱：CSV 里的 /pb/account 常默认跳这里）
    /pb/school.html  校园招聘  ←  要
故本基类**强制走 /pb/school.html**，从 careers_url 解析 host + SU<id> 拼出校招列表页。
校招列表项 DOM：
    <div class="list-row-item">
      <div class="list-cell pos-name"><span class="list-cell-span">岗位标题</span></div>
      <div class="list-cell pos-cate"><span>职位类别</span></div>
      ...（工作地点 / 招聘人数 / 更新日期）
岗位列表优先使用官方 API 返回的 postId 生成稳定详情 URL；只有 API 不可用时，
HTML 回退才使用确定性摘要作为观察记录，并明确标记分页未验证。

注：部分公司 hotjob 只有社招、校招在自建站（如 TCL → campus.tcl.com），
其 /pb/school.html 抓不到岗位会返回空（优雅降级，归 Phase 3 手工）。
"""
import hashlib
import inspect
import json
import logging
import re
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler, effective_crawl_timeout_seconds
from .render import render_page

logger = logging.getLogger(__name__)


def _clean_detail_text(value: object) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def _hotjob_host(value: object) -> str:
    try:
        parsed = urlparse(str(value or ""))
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == "hotjob.cn" or host.endswith(".hotjob.cn"):
        return host
    return ""


def _hotjob_suite_key_from_url(value: object) -> str:
    match = re.search(r"/(SU[0-9a-fA-F]+)(?:/|$)", urlparse(str(value or "")).path)
    return match.group(1) if match else ""


def _hotjob_identity_values(
    sources: list[dict], *keys: str,
) -> tuple[list[str], bool]:
    values: list[str] = []
    invalid = False
    for source in sources:
        for key in keys:
            if key not in source:
                continue
            value = source.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                invalid = True
                continue
            text = str(value).strip()
            if text:
                values.append(text)
    return values, invalid


def _hotjob_content_field(
    mapping: dict, keys: tuple[str, ...],
) -> tuple[str, bool, bool]:
    """Return cleaned text, field-present, and wrong-type flags for one JD field."""

    present = False
    invalid = False
    values: list[str] = []
    for key in keys:
        if key not in mapping:
            continue
        present = True
        value = mapping.get(key)
        if not isinstance(value, str):
            invalid = True
            continue
        values.append(_clean_detail_text(value))
    return next((value for value in values if value), ""), present, invalid


def _hotjob_identity_text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _hotjob_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _hotjob_route_reasons(
    urls: list[str], *, expected_host: str, expected_tenant: str,
) -> list[str]:
    reasons: list[str] = []
    for observed_url in urls:
        if not observed_url:
            continue
        observed_host = _hotjob_host(observed_url)
        if not observed_host:
            reasons.append("response_host_uncontrolled")
        elif observed_host != expected_host:
            reasons.append("response_host_mismatch")
        if _hotjob_suite_key_from_url(observed_url).casefold() != expected_tenant.casefold():
            reasons.append("response_tenant_mismatch")
        if urlparse(observed_url).path.rstrip("/") != (
            f"/wecruit/positionInfo/listPositionDetail/{expected_tenant}"
        ):
            reasons.append("response_api_route_mismatch")
    return list(dict.fromkeys(reasons))


def _hotjob_capture_evidence(
    *,
    detail: str,
    detail_url: str,
    request_url: str,
    response_url: str,
    request_identity: dict[str, str],
    response_identity: dict[str, str],
    response_state: str,
    response_status: str,
    identity_verified: bool,
    terminal_observed: bool,
    failure_reasons: list[str],
) -> dict[str, object]:
    content_sha256 = hashlib.sha256(detail.encode("utf-8")).hexdigest() if detail else ""
    terminal = {
        "observed": bool(terminal_observed),
        "response_state": response_state,
        "response_status": response_status,
    }
    binding = {
        "content_sha256": content_sha256,
        "source_url": detail_url,
        "request_url": request_url,
        "response_url": response_url,
        "request_identity": request_identity,
        "response_identity": response_identity,
        "terminal": terminal,
    }
    receipt_sha256 = hashlib.sha256(
        json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": (
            "complete"
            if terminal_observed and identity_verified and not failure_reasons
            else "incomplete"
        ),
        "method": "official_api",
        "source": "hotjob_api",
        "source_url": detail_url,
        "identity_verified": bool(identity_verified),
        "terminal_observed": bool(terminal_observed),
        "remaining_controls": [],
        "content_sha256": content_sha256,
        "request_url": request_url,
        "response_url": response_url,
        "request_identity": dict(request_identity),
        "response_identity": dict(response_identity),
        "response_state": response_state,
        "response_status": response_status,
        "failure_reasons": list(dict.fromkeys(failure_reasons)),
        "receipt_sha256": receipt_sha256,
    }


def _fetch_hotjob_position_detail(
    url: str, *, timeout_seconds: float = 20, capture_evidence: bool = False,
    expected_post_id: str = "", expected_title: str = "",
) -> tuple[str, str, str] | tuple[str, str, str, dict[str, object]]:
    """Return detail text, canonical URL, and active/closed/error status."""
    parsed = urlparse(url)
    suite_match = re.search(r"/(SU[0-9a-fA-F]+)", parsed.path)
    post_id = parsed.fragment or parse_qs(parsed.query).get("postId", [""])[0]
    if not suite_match or not post_id:
        if capture_evidence:
            return "", "", "error", _hotjob_capture_evidence(
                detail="",
                detail_url="",
                request_url="",
                response_url="",
                request_identity={},
                response_identity={},
                response_state="",
                response_status="error",
                identity_verified=False,
                terminal_observed=False,
                failure_reasons=["request_identity_missing"],
            )
        return "", "", "error"

    suite_key = suite_match.group(1)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    list_url = f"{origin}/{suite_key}/pb/school.html"
    detail_url = (
        f"{origin}/{suite_key}/pb/posDetail.html?"
        f"postId={quote(post_id)}&postType=campus"
    )
    api = f"{origin}/wecruit/positionInfo/listPositionDetail/{suite_key}"
    requested_post_id = str(expected_post_id or post_id).strip()
    requested_title = str(expected_title or "").strip()
    request_host = _hotjob_host(url) or (parsed.hostname or "").casefold()
    request_identity = {
        "host": request_host,
        "tenant": suite_key,
        "post_id": requested_post_id,
        "title": requested_title,
    }
    if capture_evidence and (
        not _hotjob_host(url)
        or requested_post_id != str(post_id).strip()
        or _hotjob_suite_key_from_url(url).casefold() != suite_key.casefold()
    ):
        reasons = []
        if not _hotjob_host(url):
            reasons.append("request_host_uncontrolled")
        if requested_post_id != str(post_id).strip():
            reasons.append("request_post_id_mismatch")
        if _hotjob_suite_key_from_url(url).casefold() != suite_key.casefold():
            reasons.append("request_tenant_mismatch")
        return "", detail_url, "error", _hotjob_capture_evidence(
            detail="",
            detail_url=detail_url,
            request_url=api,
            response_url="",
            request_identity=request_identity,
            response_identity={},
            response_state="",
            response_status="error",
            identity_verified=False,
            terminal_observed=False,
            failure_reasons=reasons,
        )
    response = None
    try:
        response = requests.post(
            api,
            params={
                "iSaJAx": "isAjax",
                "request_locale": "zh_CN",
                "t": str(int(time.time() * 1000)),
            },
            data={"postId": post_id},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": list_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=max(0.05, timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("state")) != "200":
            message = str(payload.get("msg") or "")
            status = "closed" if ("关闭" in message or "下架" in message) else "error"
            if capture_evidence:
                response_url = str(getattr(response, "url", "") or api)
                redirect_reasons = _hotjob_route_reasons(
                    [
                        *(
                            str(getattr(previous, "url", "") or "")
                            for previous in (getattr(response, "history", None) or [])
                        ),
                        response_url,
                    ],
                    expected_host=request_host,
                    expected_tenant=suite_key,
                )
                return "", detail_url, status, _hotjob_capture_evidence(
                    detail="",
                    detail_url=detail_url,
                    request_url=api,
                    response_url=response_url,
                    request_identity=request_identity,
                    response_identity={},
                    response_state=str(payload.get("state") or ""),
                    response_status=status,
                    identity_verified=False,
                    terminal_observed=False,
                    failure_reasons=[*redirect_reasons, f"response_{status}"],
                )
            return "", detail_url, status
        detail = payload.get("data") or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hotjob position detail failed %s: %s", url, exc)
        if capture_evidence:
            response_url = str(getattr(response, "url", "") or api)
            return "", detail_url, "error", _hotjob_capture_evidence(
                detail="",
                detail_url=detail_url,
                request_url=api,
                response_url=response_url,
                request_identity=request_identity,
                response_identity={},
                response_state="",
                response_status="error",
                identity_verified=False,
                terminal_observed=False,
                failure_reasons=["detail_request_failed"],
            )
        return "", detail_url, "error"

    if capture_evidence:
        response_url = str(getattr(response, "url", "") or api)
        response_urls = [
            *(
                str(getattr(previous, "url", "") or "")
                for previous in (getattr(response, "history", None) or [])
            ),
            response_url,
        ]
        redirect_reasons = _hotjob_route_reasons(
            response_urls,
            expected_host=request_host,
            expected_tenant=suite_key,
        )

        if not isinstance(detail, dict):
            return "", detail_url, "error", _hotjob_capture_evidence(
                detail="",
                detail_url=detail_url,
                request_url=api,
                response_url=response_url,
                request_identity=request_identity,
                response_identity={},
                response_state="200",
                response_status="error",
                identity_verified=False,
                terminal_observed=False,
                failure_reasons=[*redirect_reasons, "response_data_invalid"],
            )

        identity_sources = [detail]
        for nested_key in ("position", "positionInfo", "post", "job"):
            nested = detail.get(nested_key)
            if isinstance(nested, dict):
                identity_sources.append(nested)
        post_ids, post_id_invalid = _hotjob_identity_values(
            identity_sources, "postId", "postID", "postid", "post_id"
        )
        position_ids, position_id_invalid = _hotjob_identity_values(
            identity_sources, "positionId", "positionID", "positionid", "position_id"
        )
        external_keys, external_key_invalid = _hotjob_identity_values(
            identity_sources, "externalKey", "external_key"
        )
        internal_ids, internal_id_invalid = _hotjob_identity_values(
            identity_sources, "id", "ID"
        )
        titles, title_invalid = _hotjob_identity_values(
            identity_sources,
            "postName", "positionName", "positionTitle", "title", "jobTitle", "jobName",
        )
        tenants, tenant_invalid = _hotjob_identity_values(
            identity_sources, "suiteKey", "suiteID", "suiteId", "tenant", "tenantId", "SU", "su"
        )

        identity_namespaces = (
            ("post_id", post_ids, post_id_invalid),
            ("position_id", position_ids, position_id_invalid),
            ("external_key", external_keys, external_key_invalid),
            ("internal_id", internal_ids, internal_id_invalid),
            ("title", titles, title_invalid),
            ("tenant", tenants, tenant_invalid),
        )
        identity_reasons = list(redirect_reasons)
        for namespace, values, invalid in identity_namespaces:
            if invalid:
                identity_reasons.append(f"response_{namespace}_invalid_type")
            if len({_hotjob_identity_text(value) for value in values}) > 1:
                identity_reasons.append(f"response_{namespace}_conflict")

        observed_post_id = ""
        observed_id_namespace = ""
        for namespace, values in (
            ("post_id", post_ids),
            ("position_id", position_ids),
            ("external_key", external_keys),
            ("internal_id", internal_ids),
        ):
            if values:
                observed_id_namespace = namespace
                observed_post_id = values[0]
                break
        observed_title = titles[0] if titles else ""
        observed_tenant = tenants[0] if tenants else ""
        response_identity = {
            "host": (urlparse(response_url).hostname or "").casefold(),
            "tenant": observed_tenant or _hotjob_suite_key_from_url(response_url),
            "post_id": observed_post_id,
            "title": observed_title,
            "id_namespace": observed_id_namespace,
        }
        if not observed_post_id:
            identity_reasons.append("response_post_id_missing")
        elif _hotjob_identity_text(observed_post_id) != _hotjob_identity_text(requested_post_id):
            identity_reasons.append("response_post_id_mismatch")
        if (
            requested_title
            and observed_title
            and _hotjob_identity_text(observed_title) != _hotjob_identity_text(requested_title)
        ):
            identity_reasons.append("response_title_mismatch")
        if observed_tenant and _hotjob_identity_text(observed_tenant) != _hotjob_identity_text(suite_key):
            identity_reasons.append("response_tenant_mismatch")
        identity_verified = not identity_reasons and bool(observed_post_id)

    if not capture_evidence:
        duties = _clean_detail_text(
            detail.get("workContent")
            or detail.get("jobDescription")
            or detail.get("positionDescription")
        )
        requirements = _clean_detail_text(
            detail.get("serviceCondition")
            or detail.get("requirements")
            or detail.get("qualification")
        )
        extra = _clean_detail_text(detail.get("applyPositionContent"))
        parts = []
        if duties:
            parts.extend(["职位描述", duties])
        if requirements:
            parts.extend(["任职要求", requirements])
        if extra:
            parts.extend(["补充说明", extra])
        return "\n".join(parts)[:12000], detail_url, "active"

    duties, duties_present, duties_invalid = _hotjob_content_field(
        detail, ("workContent", "jobDescription", "positionDescription")
    )
    requirements, requirements_present, requirements_invalid = _hotjob_content_field(
        detail, ("serviceCondition", "requirements", "qualification")
    )
    extra, _, extra_invalid = _hotjob_content_field(
        detail, ("applyPositionContent",)
    )
    parts = []
    if duties:
        parts.extend(["职位描述", duties])
    if requirements:
        parts.extend(["任职要求", requirements])
    if extra:
        parts.extend(["补充说明", extra])
    raw_detail = "\n".join(parts)

    truncated = any(
        _hotjob_bool(detail.get(key))
        for key in ("truncated", "isTruncated", "contentTruncated", "hasMoreContent")
    )
    failure_reasons = list(identity_reasons)
    if not duties_present:
        failure_reasons.append("duties_field_missing")
    if not requirements_present:
        failure_reasons.append("requirements_field_missing")
    if duties_invalid or requirements_invalid or extra_invalid:
        failure_reasons.append("content_field_invalid_type")
    if truncated:
        failure_reasons.append("content_truncated")
    complete = bool(raw_detail and identity_verified and not failure_reasons)
    terminal_observed = bool(complete)
    capture_status = "active" if complete else "error"
    evidence = _hotjob_capture_evidence(
        detail=raw_detail,
        detail_url=detail_url,
        request_url=api,
        response_url=response_url,
        request_identity=request_identity,
        response_identity=response_identity,
        response_state="200",
        response_status=capture_status,
        identity_verified=identity_verified,
        terminal_observed=terminal_observed,
        failure_reasons=failure_reasons,
    )
    if complete:
        evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
    return raw_detail, detail_url, capture_status, evidence


def fetch_hotjob_position_detail(url: str) -> tuple[str, str]:
    """Fetch one public Hotjob position detail and its canonical browser URL."""
    detail, detail_url, _status = _fetch_hotjob_position_detail(url)
    return detail, detail_url


class HotjobRecruitCrawler(BaseCrawler):
    EXTRA_WAIT_MS = 6000
    SCROLL_TIMES = 6
    DETAIL_WORKERS = 8
    PAGE_SIZE = 50
    MAX_PAGES = 100
    JD_RAW_LIMIT = 12000
    _SKIP_TITLES = {"职位名称", "岗位名称"}  # 表头行

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._crawl_deadline: float | None = None
        self._reset_evidence()

    def _remaining_seconds(self, fallback: float) -> float:
        if self._crawl_deadline is None:
            return max(0.05, fallback)
        return max(0.05, min(fallback, self._crawl_deadline - time.monotonic()))

    def _budget_exhausted(self) -> bool:
        return self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline

    def _reset_evidence(self):
        """Reset runner-facing evidence for a fresh, single read-only crawl."""

        self.pagination_complete = False
        self.pages_seen = 0
        self.pages_fetched = 0
        self.page_count = 0
        self.total_pages = None
        self.expected_pages = None
        self.has_more = False
        self.advertised_total = None
        self.expected_total = None
        self.total_count = None
        self.raw_listed_count = 0
        self.listed_count = 0
        self.unique_listed_count = 0
        self.duplicate_ids = []
        self.invalid_row_count = 0
        self.pagination_termination_reason = "not_started"
        self.fetch_failed = False
        self.resolved_source_url = ""
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures = []
        self.completeness_evidence = {}
        self.metrics = {}
        self.api_attempted = False
        self.api_valid_response = False

    def _set_termination(self, reason: str, has_more: bool | None = None):
        self.pagination_termination_reason = reason
        if has_more is not None:
            self.has_more = has_more

    def _update_evidence(self):
        self.completeness_evidence = {
            "source_url": self.careers_url,
            "effective_source_url": self.resolved_source_url or self.careers_url,
            "api_url": self._api_url(),
            "pages_seen": self.pages_seen,
            "pages_fetched": self.pages_fetched,
            "total_pages": self.total_pages,
            "expected_pages": self.expected_pages,
            "advertised_total": self.advertised_total,
            "expected_total": self.expected_total,
            "raw_listed_count": self.raw_listed_count,
            "listed_count": self.listed_count,
            "unique_listed_count": self.unique_listed_count,
            "duplicate_ids": list(self.duplicate_ids),
            "invalid_row_count": self.invalid_row_count,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
            "read_only": True,
        }
        self.metrics = {
            "pagination": dict(self.completeness_evidence),
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
            "detail_complete": self.detail_complete,
        }

    def _base(self) -> str:
        p = urlparse(self.careers_url)
        m = re.search(r"/(SU[0-9a-fA-F]+)", p.path)
        return f"https://{p.netloc}/{m.group(1) if m else ''}"

    def _suite_key(self) -> str:
        m = re.search(r"/(SU[0-9a-fA-F]+)", urlparse(self.careers_url).path)
        return m.group(1) if m else ""

    def _discover_suite_url(self) -> str:
        """Resolve a root Hotjob tenant to its advertised SU suite without guessing."""

        documents: list[tuple[str, str]] = []
        if self._budget_exhausted():
            return ""
        try:
            response = requests.get(
                self.careers_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self._remaining_seconds(20),
            )
            response.raise_for_status()
            documents.append((response.url, response.text))
        except requests.RequestException as exc:
            logger.debug("[%s] hotjob 根入口请求失败: %s", self.company_name, exc)
        if not documents or not re.search(r"/SU[0-9a-fA-F]+", " ".join(item[1] for item in documents)):
            html = render_page(
                self.careers_url,
                wait_for=None,
                timeout_ms=30000,
                extra_wait_ms=1500,
                scroll_times=1,
            )
            if html:
                documents.append((self.careers_url, html))
        for base_url, html in documents:
            candidates = [base_url]
            soup = BeautifulSoup(html, "html.parser")
            candidates.extend(
                urljoin(base_url, str(node.get("href") or ""))
                for node in soup.find_all("a", href=True)
            )
            candidates.extend(
                urljoin(base_url, match.group(0))
                for match in re.finditer(r"/SU[0-9a-fA-F]+(?:/[^\"'<>\s]*)?", html)
            )
            for candidate in candidates:
                if re.search(r"/SU[0-9a-fA-F]+", urlparse(candidate).path):
                    return candidate
        return ""

    def _api_url(self) -> str:
        suite_key = self._suite_key()
        if not suite_key:
            return ""
        parsed = urlparse(self.careers_url)
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        return f"{origin}/wecruit/positionInfo/listPosition/{suite_key}"

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _optional_bool(value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().casefold()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return None

    @staticmethod
    def _stable_key(*parts: object) -> str:
        payload = "\x1f".join(str(part or "").strip() for part in parts)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _detail_url(self, post_id: object) -> str:
        parsed = urlparse(self.careers_url)
        origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        suite_key = self._suite_key()
        return (
            f"{origin}/{suite_key}/pb/posDetail.html?"
            f"postId={quote(str(post_id), safe='')}&postType=campus"
        )

    def _attach_identity(self, job: dict, source_id: str, list_url: str):
        prefix = self._suite_key() or urlparse(list_url).netloc or "unknown"
        job.update(
            {
                "id": f"hotjob:{prefix}:{source_id}",
                "source_job_id": source_id,
                "source_id": source_id,
                "external_id": source_id,
                "source_list_url": list_url,
                "source_read_only": True,
            }
        )

    def _element_source_id(self, element, title: str, city: str, list_url: str) -> str:
        for attr in ("data-post-id", "data-postid", "data-id", "postId", "postid"):
            value = str(element.get(attr) or "").strip()
            if value:
                return value
        for anchor in element.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            parsed = urlparse(href)
            query_id = parse_qs(parsed.query).get("postId", [""])[0]
            if query_id:
                return str(query_id).strip()
            if parsed.fragment:
                return parsed.fragment.strip()
        return f"html-{self._stable_key(list_url, title, city)}"

    def _make_static_job(
        self,
        title: str,
        city: str,
        list_url: str,
        source_id: str,
        jd_raw: str = "",
    ) -> dict:
        key = quote(source_id, safe="")
        job = self._make_job(
            title=title,
            city=city[:40],
            jd_url=f"{list_url}#{key}",
            jd_raw=jd_raw[: self.JD_RAW_LIMIT],
            link_kind="list",
        )
        self._attach_identity(job, source_id, list_url)
        return job

    def _list_url(self) -> str:  # 桌面校招页
        return f"{self._base()}/pb/school.html"

    def _mc_url(self) -> str:  # 移动校招页（部分租户只有 /mc/，桌面 /pb/ 为 404）
        return f"{self._base()}/mc/position/campus"

    def _foxconn_url(self) -> str:
        p = urlparse(self.careers_url)
        if "foxconn.hotjob.cn" not in p.netloc:
            return ""
        return "https://foxconn.hotjob.cn/wt/Foxconn/web/index/CompFoxconnPagerecruit_School"

    @staticmethod
    def _cell_text(row, *suffixes: str) -> str:
        """取 row 里 class 形如 list-cell pos-<suffix> 的单元格内层 span 文本（避开"热招"等 badge）。"""
        for suf in suffixes:
            cell = row.find(
                lambda t: t.has_attr("class")
                and "list-cell" in t["class"]
                and any(f"pos-{suf}" in c for c in t["class"])
            )
            if cell:
                span = cell.find("span", class_="list-cell-span")
                return (span or cell).get_text(" ", strip=True)
        return ""

    def _parse_pb(self, html: str, list_url: str) -> list[dict]:
        """桌面 /pb/school.html：表格 list-row-item，pos-name/pos-locate 单元格。"""
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all(
            lambda t: t.has_attr("class") and any("list-row-item" in c for c in t["class"])
        )
        jobs, seen = [], set()
        for row in rows:
            title = self._cell_text(row, "name")
            if not title or len(title) < 2 or title in self._SKIP_TITLES:
                continue
            city = self._cell_text(row, "locate", "area", "location", "city", "addr")
            source_id = self._element_source_id(row, title, city, list_url)
            if source_id in seen:
                continue
            seen.add(source_id)
            jobs.append(self._make_static_job(title, city, list_url, source_id))
        return jobs

    def _parse_pb_cards(self, html: str, list_url: str) -> list[dict]:
        """新版 /pb/school.html：卡片 list-card-item1，标题 pos-title-item。"""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(
            lambda t: t.has_attr("class") and any("list-card-item" in c for c in t["class"])
        )
        jobs, seen = [], set()
        for card in cards:
            title_el = card.find(
                lambda t: t.has_attr("class") and any("pos-title-item" in c for c in t["class"])
            )
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title or len(title) < 2 or title in self._SKIP_TITLES:
                continue
            text = card.get_text(" ", strip=True)
            city = ""
            for part in [p.strip() for p in re.split(r"[|｜]", text) if p.strip()]:
                if re.search(r"(市|省|区|县|州|盟|香港|澳门|台湾)", part) and "更新日期" not in part:
                    city = part
                    break
            source_id = self._element_source_id(card, title, city, list_url)
            if source_id in seen:
                continue
            seen.add(source_id)
            jobs.append(self._make_static_job(title, city, list_url, source_id, text))
        return jobs

    @classmethod
    def _first_api_int(cls, mapping: object, *keys: str) -> int | None:
        if not isinstance(mapping, dict):
            return None
        for key in keys:
            value = cls._as_int(mapping.get(key))
            if value is not None:
                return value
        return None

    @classmethod
    def _api_has_more(cls, page_form: dict, data: dict) -> bool | None:
        for mapping in (page_form, data):
            for key in ("hasNextPage", "hasNext", "has_more", "hasMore", "more"):
                if key in mapping:
                    value = cls._optional_bool(mapping.get(key))
                    if value is not None:
                        return value
        return None

    @classmethod
    def _api_post_id(cls, item: dict) -> str:
        for key in ("postId", "externalKey", "positionId", "id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    def _make_api_job(self, item: dict, list_url: str) -> dict | None:
        title = str(item.get("postName") or item.get("positionName") or "").strip()
        post_id = self._api_post_id(item)
        if not title or not post_id:
            return None
        city = str(item.get("workPlaceStr") or item.get("workPlace") or "").strip()
        jd_raw = " | ".join(
            str(value)
            for value in (
                item.get("postTypeName"),
                item.get("company"),
                item.get("department"),
                item.get("projectName"),
                item.get("educationStr"),
            )
            if value
        )
        campaign_text = str(item.get("projectName") or "校园招聘").strip()
        job = self._make_job(
            title=title,
            city=city[:40],
            job_type=str(item.get("postTypeName") or "校招").strip(),
            jd_url=self._detail_url(post_id),
            jd_raw=jd_raw[: self.JD_RAW_LIMIT],
            published_at=str(item.get("publishDate") or "")[:10],
            link_kind="detail",
            campaign_text=campaign_text,
        )
        self._attach_identity(job, post_id, list_url)
        job["source_post_id"] = post_id
        return job

    def _hydrate_api_jobs(self, jobs: list[dict]) -> list[dict]:
        self.detail_expected_total = len(jobs)
        if not jobs:
            self.detail_complete = True
            return []

        def hydrate(job: dict):
            detail_fetch = _fetch_hotjob_position_detail
            parameters = inspect.signature(detail_fetch).parameters
            kwargs = {}
            if "timeout_seconds" in parameters:
                kwargs["timeout_seconds"] = self._remaining_seconds(20)
            if "capture_evidence" in parameters:
                kwargs.update(
                    capture_evidence=True,
                    expected_post_id=str(
                        job.get("source_post_id") or job.get("source_job_id") or ""
                    ).strip(),
                    expected_title=str(job.get("title") or "").strip(),
                )
            result = detail_fetch(job["jd_url"], **kwargs)
            capture_evidence = None
            if isinstance(result, tuple) and len(result) == 4:
                detail, detail_url, status, candidate_evidence = result
                if isinstance(candidate_evidence, dict):
                    capture_evidence = candidate_evidence
            else:  # Keep lightweight test doubles and older adapters compatible.
                detail, detail_url, status = result
            if detail:
                job["jd_raw"] = detail
            if detail_url:
                job["jd_url"] = detail_url
                job["link_kind"] = "detail"
            if capture_evidence is not None:
                job["capture_evidence"] = capture_evidence
            return job, detail_url or job["jd_url"], status, capture_evidence

        hydrated: list[dict] = []
        with ThreadPoolExecutor(max_workers=self.DETAIL_WORKERS) as executor:
            results = list(executor.map(hydrate, jobs))
        for job, detail_url, status, capture_evidence in results:
            source_id = str(job.get("source_job_id") or "").strip()
            if status == "closed":
                self.detail_failures.append(
                    {"id": source_id, "url": detail_url, "reason": "detail_closed"}
                )
                if capture_evidence is None:
                    continue
            elif status != "active":
                self.detail_failures.append(
                    {"id": source_id, "url": detail_url, "reason": "detail_request_failed"}
                )
            else:
                self.detail_count += 1
            hydrated.append(job)
        self.detail_complete = (
            self.detail_count == self.detail_expected_total and not self.detail_failures
        )
        return hydrated

    def _fetch_new_pb_api(self) -> list[dict]:
        """Fetch the read-only Hotjob list API until its contract closes."""
        self.api_attempted = True
        suite_key = self._suite_key()
        if not suite_key:
            self._set_termination("missing_suite_key", False)
            self._update_evidence()
            return []

        api = self._api_url()
        source_path = urlparse(self.careers_url).path.casefold()
        list_url = self._mc_url() if "/mc/" in source_path else self._list_url()
        self.resolved_source_url = list_url
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": list_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        jobs: list[dict] = []
        seen: set[str] = set()
        page = 1
        effective_page_size = self.PAGE_SIZE
        expected_page_size: int | None = None

        while page <= self.MAX_PAGES:
            if self._budget_exhausted():
                self.fetch_failed = True
                self._set_termination("hard_timeout", True)
                break
            params = {
                "iSaJAx": "isAjax",
                "request_locale": "zh_CN",
                "t": str(int(time.time() * 1000)),
            }
            form = {
                "isFrompb": "true",
                "recruitType": "1",
                "pageSize": str(effective_page_size),
                "currentPage": str(page),
            }
            try:
                response = requests.post(
                    api,
                    params=params,
                    data=form,
                    headers=headers,
                    timeout=self._remaining_seconds(20),
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001
                self.fetch_failed = True
                self._set_termination(
                    "hard_timeout" if self._budget_exhausted()
                    else f"api_request_failed_page_{page}",
                    True,
                )
                logger.debug("[%s] hotjob API 失败 page=%s: %s", self.company_name, page, exc)
                break

            if not isinstance(payload, dict):
                self.fetch_failed = True
                self._set_termination(f"api_payload_invalid_page_{page}", True)
                break
            state = str(payload.get("state") or "").strip().casefold()
            if state and state not in {"200", "0", "success"}:
                self.fetch_failed = True
                self._set_termination(f"api_state_invalid_page_{page}", True)
                break
            data = payload.get("data") or {}
            page_form = data.get("pageForm") if isinstance(data, dict) else None
            if not isinstance(page_form, dict):
                self.fetch_failed = True
                self._set_termination(f"api_page_form_missing_page_{page}", True)
                break
            page_data = page_form.get("pageData")
            if page_data is None:
                page_data = page_form.get("items") or []
            if not isinstance(page_data, list):
                self.fetch_failed = True
                self._set_termination(f"api_page_data_invalid_page_{page}", True)
                break
            self.api_valid_response = True

            response_page_size = self._first_api_int(page_form, "pageSize")
            if response_page_size:
                if expected_page_size is None:
                    expected_page_size = response_page_size
                    effective_page_size = response_page_size
                elif response_page_size != expected_page_size:
                    self._set_termination("api_page_size_changed", True)
                    break

            page_total = self._first_api_int(
                page_form, "totalPage", "pageCount", "totalPages"
            )
            if page_total is not None:
                if self.total_pages is None:
                    self.total_pages = page_total
                    self.expected_pages = page_total
                elif page_total != self.total_pages:
                    self._set_termination("api_total_pages_changed", True)
                    break

            advertised_total = self._first_api_int(
                page_form,
                "dataCount",
                "totalCount",
                "totalRecord",
                "recordCount",
                "total",
                "records",
            )
            if advertised_total is not None:
                if self.advertised_total is None:
                    self.advertised_total = advertised_total
                    self.expected_total = advertised_total
                    self.total_count = advertised_total
                elif advertised_total != self.advertised_total:
                    self._set_termination("api_total_count_changed", True)
                    break

            has_more = self._api_has_more(page_form, data if isinstance(data, dict) else {})
            if has_more is not None:
                self.has_more = has_more
            self.pages_seen = page
            self.pages_fetched = page
            self.page_count = page
            self.raw_listed_count += len(page_data)

            for item in page_data:
                if not isinstance(item, dict):
                    self.invalid_row_count += 1
                    continue
                post_id = self._api_post_id(item)
                if not post_id:
                    self.invalid_row_count += 1
                    continue
                if post_id in seen:
                    self.duplicate_ids.append(post_id)
                    continue
                job = self._make_api_job(item, list_url)
                if job is None:
                    self.invalid_row_count += 1
                    continue
                seen.add(post_id)
                jobs.append(job)
            self.unique_listed_count = len(seen)
            self.listed_count = len(jobs)

            if self.total_pages is not None:
                if page < self.total_pages:
                    if has_more is False:
                        self._set_termination("api_has_more_conflict", False)
                        break
                    page += 1
                    continue
                if self.advertised_total is not None:
                    if (
                        self.unique_listed_count == self.advertised_total
                        and has_more is not True
                        and not self.duplicate_ids
                        and not self.invalid_row_count
                    ):
                        self.pagination_complete = True
                        self._set_termination("api_total_pages_and_count_reached", False)
                    elif self.unique_listed_count > self.advertised_total:
                        self._set_termination("api_total_count_mismatch", False)
                    else:
                        self._set_termination("api_total_count_mismatch", False)
                elif has_more is not True and not self.duplicate_ids and not self.invalid_row_count:
                    self.pagination_complete = True
                    self._set_termination("api_total_pages_reached", False)
                else:
                    self._set_termination("api_has_more_conflict", True)
                break

            if self.advertised_total is not None:
                if (
                    self.unique_listed_count == self.advertised_total
                    and has_more is not True
                    and not self.duplicate_ids
                    and not self.invalid_row_count
                ):
                    self.pagination_complete = True
                    self._set_termination("api_total_reached", False)
                    break
                if self.unique_listed_count > self.advertised_total:
                    self._set_termination("api_total_count_mismatch", False)
                    break
                if has_more is False:
                    self._set_termination("api_total_count_mismatch", False)
                    break
                page += 1
                continue

            if has_more is not None:
                if has_more:
                    page += 1
                    continue
                if self.duplicate_ids or self.invalid_row_count:
                    self._set_termination("api_rows_invalid", False)
                else:
                    self.pagination_complete = True
                    self._set_termination("api_has_more_false", False)
                break
            if not page_data:
                if self.duplicate_ids or self.invalid_row_count:
                    self._set_termination("api_rows_invalid", False)
                else:
                    self.pagination_complete = True
                    self._set_termination("api_empty_terminal_page", False)
                break
            # With no official total or has-more flag, only a subsequent empty
            # page is an explicit termination; a short first page is not enough.
            page += 1
        else:
            self._set_termination("api_max_pages_reached", True)

        if self.pagination_complete:
            self.has_more = False
        elif self.pagination_termination_reason == "not_started":
            self._set_termination("api_pagination_incomplete", True)
        self.detail_complete = False
        hydrated = self._hydrate_api_jobs(jobs)
        self._update_evidence()
        return hydrated

    def _parse_mc(self, html: str, list_url: str) -> list[dict]:
        """移动 /mc/position/campus：div.listItem，标题 span.listItemRtTitCon。"""
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all(
            lambda t: t.has_attr("class") and any("listItem" in c for c in t["class"])
        )
        jobs, seen = [], set()
        for it in items:
            tit_el = it.find("span", class_=lambda c: c and "listItemRtTitCon" in c)
            if not tit_el:
                continue
            title = tit_el.get_text(" ", strip=True)
            if not title or len(title) < 2:
                continue
            m = re.search(r"([一-龥]{2,}[市省](?:、[一-龥]{2,}[市省])*)", it.get_text(" ", strip=True))
            city = m.group(1) if m else ""
            source_id = self._element_source_id(it, title, city, list_url)
            if source_id in seen:
                continue
            seen.add(source_id)
            jobs.append(self._make_static_job(title, city, list_url, source_id))
        return jobs

    def _parse_foxconn(self, html: str, list_url: str) -> list[dict]:
        """富士康 Dayee 模板；当前无岗位时页面/API 显示“暂无数据内容”。"""
        soup = BeautifulSoup(html, "html.parser")
        if "暂无数据" in soup.get_text(" ", strip=True):
            return []
        jobs, seen = [], set()
        for row in soup.select("tbody tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            title = cells[0]
            if title in self._SKIP_TITLES or len(title) < 2:
                continue
            city = cells[3] if len(cells) > 3 else ""
            source_id = self._element_source_id(row, title, city, list_url)
            if source_id in seen:
                continue
            seen.add(source_id)
            jobs.append(self._make_static_job(
                title, city, list_url, source_id, " | ".join(cells)
            ))
        return jobs

    def _mark_rendered_observation(self, list_url: str, jobs: list[dict]):
        """Record a rendered page as an observation, never as full pagination."""
        self.resolved_source_url = list_url
        self.pages_seen = 1
        self.pages_fetched = 1
        self.page_count = 1
        self.raw_listed_count = len(jobs)
        self.listed_count = len(jobs)
        self.unique_listed_count = len({job.get("source_job_id") for job in jobs})
        self.has_more = True
        self.pagination_complete = False
        self._set_termination("rendered_list_pagination_unverified", True)
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self._update_evidence()

    def fetch(self) -> list[dict]:
        self._reset_evidence()
        self._crawl_deadline = time.monotonic() + effective_crawl_timeout_seconds(120.0)
        self.resolved_source_url = self.careers_url
        foxconn_url = self._foxconn_url()
        if foxconn_url:
            html = render_page(foxconn_url, wait_for=None, timeout_ms=45000,
                               extra_wait_ms=self.EXTRA_WAIT_MS, scroll_times=self.SCROLL_TIMES)
            if html:
                jobs = self._parse_foxconn(html, foxconn_url)
                self._mark_rendered_observation(foxconn_url, jobs)
                logger.info("[%s] foxconn hotjob 抓到 %d 个岗位", self.company_name, len(jobs))
                return jobs
            self.fetch_failed = True
            self._set_termination("render_failed", True)
            self._update_evidence()
            return []

        if not self._suite_key():
            discovered_url = self._discover_suite_url()
            if discovered_url:
                self.careers_url = discovered_url
                self.resolved_source_url = discovered_url
            else:
                self._set_termination("missing_suite_key", False)
                self._update_evidence()
                logger.info("[%s] hotjob 根入口未发现 SU suite，安全停止", self.company_name)
                return []

        api_jobs = self._fetch_new_pb_api()
        if self.api_valid_response or self.pages_seen or api_jobs:
            logger.info("[%s] hotjob API 抓到 %d 个岗位", self.company_name, len(api_jobs))
            return api_jobs

        # 先桌面 /pb/school.html；为空再退移动 /mc/position/campus（部分租户桌面 404）
        for url, parse in ((self._list_url(), self._parse_pb), (self._mc_url(), self._parse_mc)):
            html = render_page(url, wait_for=None, timeout_ms=45000,
                               extra_wait_ms=self.EXTRA_WAIT_MS, scroll_times=self.SCROLL_TIMES)
            if not html:
                continue
            jobs = parse(html, url)
            if not jobs and "/pb/school.html" in url:
                jobs = self._parse_pb_cards(html, url)
            if jobs:
                self._mark_rendered_observation(url, jobs)
                logger.info("[%s] hotjob 抓到 %d 个岗位 (%s)", self.company_name, len(jobs),
                            "mc" if "/mc/" in url else "pb")
                return jobs
        if not self.fetch_failed:
            self.fetch_failed = True
            self._set_termination("render_failed", True)
        self._update_evidence()
        logger.info("[%s] hotjob 抓到 0 个岗位", self.company_name)
        return []
