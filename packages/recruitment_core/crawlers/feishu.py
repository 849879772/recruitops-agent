"""飞书招聘（Lark Recruitment）通用爬虫基类。

小米 / 字节 / 蔚来等都用飞书招聘 SaaS，前端 DOM 完全一致：
    <a href="/campus/position/<ID>/detail">
      <div class="positionItem">
        <div class="positionItem-title">
          <span class="positionItem-title-text">标题</span>
        </div>
        <div class="positionItem-subTitle">
          <span>城市</span> | <span>校招/实习</span> | <span>类别</span> ...
        </div>
      </div>
    </a>
分页是 client-side（`.atsx-pagination-next` 按钮，URL 不变）。

子类只需覆盖类属性（LIST_URL / HOST / MAX_PAGES …），无需重写抓取逻辑。
"""
import html as html_lib
import json
import logging
import math
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler, effective_crawl_timeout_seconds, launch_browser

logger = logging.getLogger(__name__)

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CAMPAIGN_LABEL_RE = re.compile(
    r"(?<!\d)(?:20\d{2}|[12]\d)\s*(?:届|年)?\s*"
    r"(?:春招|秋招|校招|校园招聘|校园招募|应届生招聘|毕业生招聘|"
    r"暑期实习|日常实习|实习生招聘)",
    re.I,
)


def feishu_project_path(url: object) -> str:
    """Return the shared project prefix for Feishu list or detail routes.

    Feishu tenants expose both ``/<project>/`` and
    ``/<project>/position/<job>/detail`` forms.  Keep this parser platform
    scoped so callers do not infer a project from a host-only root URL.
    """

    path = urlsplit(str(url or "")).path.rstrip("/")
    detail_match = re.search(
        r"^(?P<prefix>.*?)/position/[^/]+/detail$", path, flags=re.I
    )
    if detail_match:
        return detail_match.group("prefix").rstrip("/")
    return re.sub(r"/position(?:/(?:list|application))?$", "", path, flags=re.I).rstrip("/")


class FeishuRecruitCrawler(BaseCrawler):
    """飞书招聘站点的通用 Playwright 翻页爬虫。

    子类覆盖：
        LIST_URL        岗位列表页 URL
        HOST            拼接相对 href 用的站点根（无尾斜杠）
        MAX_PAGES       最多翻几页
        GOTO_WAIT_UNTIL goto 的 wait_until 策略（networkidle / domcontentloaded）
        GOTO_TIMEOUT_MS goto 超时
        JD_RAW_LIMIT    jd_raw 截断长度
    """

    LIST_URL = ""
    HOST = ""
    # Stop on the disabled next button or repeated page content. This is a
    # circuit breaker only, not a normal collection limit.
    MAX_PAGES = 500
    GOTO_WAIT_UNTIL = "networkidle"
    GOTO_TIMEOUT_MS = 60000
    # The public search response already carries detail fields.  500 was a
    # legacy list-card cap and silently cut otherwise usable Feishu JDs.
    JD_RAW_LIMIT = 12000
    HARD_TIMEOUT_MS = 180000
    API_PATH = "/api/v1/search/job/posts"
    API_PAGE_SIZE = 10

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_pagination_state()

    def _reset_pagination_state(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.pages_fetched = 0
        self.advertised_total = None
        self.expected_total = None
        self.total_pages = None
        self.has_more = False
        self.resolved_source_url = ""
        self.fetch_failed = False
        self.pagination_evidence: list[dict] = []

    @staticmethod
    def _clean_url(value: str) -> str:
        return html_lib.unescape(str(value or "")).strip()

    @staticmethod
    def _json_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and value.is_integer() and value >= 0:
            return int(value)
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if value.isdigit():
                return int(value)
        return None

    @classmethod
    def _text_value(cls, value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in (
                "name", "label", "text", "value", "zh_cn", "zh-CN",
                "zhName", "zh_name", "i18n_name", "cityName",
            ):
                text = cls._text_value(value.get(key))
                if text:
                    return text
        if isinstance(value, (list, tuple)):
            return ", ".join(
                text for text in (cls._text_value(item) for item in value) if text
            )
        return ""

    @classmethod
    def _configured_job_paths(cls, html: str) -> list[str]:
        """Read the public website config when a share/mobile entry hides the list route."""
        soup = BeautifulSoup(html or "", "html.parser")
        node = soup.select_one("#js-websiteInfo")
        if node is None:
            return []
        raw = node.get_text("", strip=True) or node.get("data-value") or ""
        try:
            payload = json.loads(html_lib.unescape(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        website_info = payload.get("website_info") or payload.get("websiteInfo") or payload
        if not isinstance(website_info, dict):
            return []
        config = website_info.get("web_ui_config") or website_info.get("webUiConfig")
        if isinstance(config, str):
            try:
                config = json.loads(html_lib.unescape(config))
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if not isinstance(config, dict):
            return []
        page_configs = config.get("pageConfigs") or config.get("page_configs") or []
        if isinstance(page_configs, dict):
            page_configs = list(page_configs.values())
        paths = []
        for item in page_configs:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("route") or item.get("url")
            if isinstance(path, str) and "position" in path.lower() and "share" not in path.lower():
                paths.append(path)
        return paths

    @classmethod
    def _candidate_list_urls(cls, careers_url: str, html: str = "") -> list[str]:
        """Expand root, mobile, application, referral and share links to list routes."""
        cleaned = cls._clean_url(careers_url)
        parts = urlsplit(cleaned)
        if not parts.scheme or not parts.netloc:
            return []

        source_query = parts.query
        source_segments = [segment for segment in parts.path.split("/") if segment]
        lower_segments = [segment.lower() for segment in source_segments]
        is_share_entry = any(
            segment in {"referral", "share", "notoken", "application"}
            for segment in lower_segments
        )

        route_segments = list(source_segments)
        if route_segments and route_segments[0].lower() == "referral":
            route_segments = route_segments[1:]
        route_segments = [segment for segment in route_segments if segment.lower() != "m"]
        for marker in ("application", "share", "notoken"):
            if marker in [segment.lower() for segment in route_segments]:
                route_segments = route_segments[:[segment.lower() for segment in route_segments].index(marker)]
                break
        if "position" in [segment.lower() for segment in route_segments]:
            position_index = [segment.lower() for segment in route_segments].index("position")
            prefix_segments = route_segments[:position_index]
        else:
            prefix_segments = route_segments
        prefix = "/" + "/".join(prefix_segments) if prefix_segments else ""

        candidates: list[str] = []
        seen: set[str] = set()

        def add(path: str, query: str = source_query) -> None:
            path = re.sub(r"/{2,}", "/", "/" + path.strip("/"))
            if path == "/":
                return
            value = urlunsplit((parts.scheme, parts.netloc, path, query, ""))
            if value not in seen:
                seen.add(value)
                candidates.append(value)

        # A direct list URL is the strongest candidate, after removing a mobile marker.
        if not is_share_entry and any(segment.lower() == "position" for segment in source_segments):
            direct_segments = [segment for segment in source_segments if segment.lower() != "m"]
            add("/" + "/".join(direct_segments))

        for configured_path in cls._configured_job_paths(html):
            config_parts = urlsplit(cls._clean_url(configured_path))
            config_path = config_parts.path or cls._clean_url(configured_path)
            if config_path.startswith("/"):
                add(config_path, config_parts.query or source_query)
            elif prefix:
                add(f"{prefix}/{config_path}", config_parts.query or source_query)

        if prefix:
            add(f"{prefix}/position/list")
            add(f"{prefix}/position")

        # Referral pages can be valid landing pages without a tenant-specific prefix.
        # Keep these same-host fallbacks read-only and bounded; the first working route wins.
        for fallback_prefix in ("/campus", "/campusrecruitment", "/campus_recruitment"):
            add(f"{fallback_prefix}/position/list")
            add(f"{fallback_prefix}/position")
        return candidates

    @staticmethod
    def _route_prefix(list_url: str) -> str:
        return feishu_project_path(list_url)

    def _detail_url(self, list_url: str, row: dict) -> str:
        raw = self._text_value(
            row.get("detail_url") or row.get("detailUrl") or row.get("job_url")
            or row.get("jobUrl") or row.get("url")
        )
        if raw and "/position/" in raw and "/detail" in raw:
            if raw.startswith("http"):
                value = raw
            elif raw.startswith("/"):
                prefix = self._route_prefix(list_url)
                origin = f"{urlsplit(list_url).scheme}://{urlsplit(list_url).netloc}"
                value = origin + (raw if raw.startswith(prefix + "/") else prefix + raw)
            else:
                value = urljoin(list_url, raw)
            parsed = urlsplit(value)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

        job_id = self._text_value(
            row.get("id") or row.get("job_id") or row.get("jobId") or row.get("position_id")
        )
        if not job_id:
            return ""
        parts = urlsplit(list_url)
        path = f"{self._route_prefix(list_url)}/position/{job_id}/detail"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _api_page_url(url: str, offset: int, limit: int) -> str:
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params["offset"] = str(offset)
        params["limit"] = str(limit)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))

    @staticmethod
    def _next_button(page):
        locator = page.locator(".atsx-pagination-next")
        if locator.count() == 0:
            return None
        return locator.first

    def _parse_page_jobs(self, page) -> list[dict]:
        soup = BeautifulSoup(page.content(), "html.parser")
        anchors = [
            anchor for anchor in soup.find_all("a", href=True)
            if "/position/" in self._clean_url(anchor["href"])
            and "/detail" in self._clean_url(anchor["href"])
        ]
        return self._parse_anchors(anchors)

    def _parse_anchors(self, anchors) -> list[dict]:
        jobs = []
        for a in anchors:
            title_el = a.select_one(".positionItem-title-text")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or len(title) < 2:
                continue

            city = ""
            sub = a.select_one(".positionItem-subTitle")
            if sub:
                first_span = sub.find("span")
                if first_span:
                    city = first_span.get_text(strip=True)

            href = self._clean_url(a["href"])
            if not href.startswith(("http://", "https://")):
                href = urljoin(self.HOST + "/", href)
            parts = urlsplit(href)
            href = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

            # A DOM anchor is a list card, not authoritative job detail.  Keep
            # campaign evidence, but do not persist recommendation/list text as
            # the JD; the detail route/API is hydrated separately.
            card_text = a.get_text(separator=" ", strip=True)
            campaign_text = " ".join(dict.fromkeys(
                match.group(0).strip()
                for match in _CAMPAIGN_LABEL_RE.finditer(card_text)
            ))

            jobs.append(
                self._make_job(
                    title=title,
                    city=city,
                    jd_url=href,
                    jd_raw="",
                    campaign_text=campaign_text,
                )
            )
        return jobs

    @classmethod
    def _advertised_total_from_text(cls, html: str) -> int | None:
        text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
        patterns = (
            r"(?:开启新的工作|职位|岗位|工作)\s*[（(]\s*([\d,]+)",
            r"(?:共|合计)\s*([\d,]+)\s*(?:个|条)?\s*(?:职位|岗位|工作)",
            r"([\d,]+)\s*(?:个|条)\s*(?:职位|岗位|工作)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return cls._json_int(match.group(1))
        return None

    def _parse_api_payload(self, payload: object, list_url: str,
                           response_url: str = "", request_payload: dict | None = None) -> dict | None:
        """Normalize the public search/job/posts response without mutating it."""
        if not isinstance(payload, dict):
            return None
        if payload.get("code") not in (None, 0, "0"):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None

        rows = data.get("job_post_list") or data.get("jobPostList") or data.get("jobs")
        if rows is None:
            rows = data.get("list") or data.get("rows")
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("rows") or rows.get("items")
        if not isinstance(rows, list):
            return None

        total = None
        for container in (data, payload):
            for key in ("count", "total", "total_count", "totalCount"):
                total = self._json_int(container.get(key))
                if total is not None:
                    break
            if total is not None:
                break

        explicit_has_more = False
        has_more = None
        for container in (data, payload):
            for key in ("hasMore", "has_more", "more"):
                if key in container and isinstance(container[key], (bool, int)):
                    has_more = bool(container[key])
                    explicit_has_more = True
                    break
            if explicit_has_more:
                break

        query = parse_qs(urlsplit(response_url).query)
        request_payload = request_payload if isinstance(request_payload, dict) else {}
        offset = self._json_int(request_payload.get("offset"))
        limit = self._json_int(request_payload.get("limit"))
        if offset is None:
            offset = self._json_int((query.get("offset") or [None])[0])
        if limit is None:
            limit = self._json_int((query.get("limit") or [None])[0])
        offset = offset if offset is not None else 0
        limit = limit if limit is not None else (len(rows) or self.API_PAGE_SIZE)
        if has_more is None:
            has_more = total is None or offset + len(rows) < total

        jobs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = self._text_value(
                row.get("title") or row.get("job_title") or row.get("jobTitle")
            )
            jd_url = self._detail_url(list_url, row)
            if not title or len(title) < 2 or not jd_url:
                continue

            city_values = row.get("city_list") or row.get("cityList") or row.get("city")
            city = self._text_value(city_values)
            type_values = [
                self._text_value(row.get("recruit_type") or row.get("recruitType")),
                self._text_value(row.get("job_category") or row.get("jobCategory")),
            ]
            job_type = " ".join(dict.fromkeys(value for value in type_values if value)) or "校招"
            raw_parts = [
                self._text_value(row.get(key))
                for key in (
                    "description", "requirement", "responsibility", "job_description",
                    "jobDescription", "address_list", "addressList",
                )
            ]
            jd_raw = "\n".join(dict.fromkeys(value for value in raw_parts if value))
            published_at = ""
            publish_time = row.get("publish_time") or row.get("publishTime")
            if isinstance(publish_time, (int, float)) and publish_time:
                timestamp = publish_time / 1000 if publish_time > 10_000_000_000 else publish_time
                try:
                    published_at = datetime.fromtimestamp(timestamp).date().isoformat()
                except (OverflowError, OSError, ValueError):
                    published_at = ""
            campaign_text = " ".join(dict.fromkeys(
                match.group(0).strip()
                for match in _CAMPAIGN_LABEL_RE.finditer(jd_raw)
            ))
            jobs.append(self._make_job(
                title=title,
                city=city,
                job_type=job_type,
                jd_url=jd_url,
                jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                published_at=published_at,
                campaign_text=campaign_text,
            ))

        return {
            "jobs": jobs,
            "total": total,
            "has_more": has_more,
            "explicit_has_more": explicit_has_more,
            "offset": offset,
            "limit": limit,
            "raw_count": len(rows),
        }

    @staticmethod
    def _button_disabled(button) -> bool:
        try:
            if button.is_disabled():
                return True
        except Exception:
            pass
        for name in ("class", "aria-disabled", "disabled"):
            try:
                value = button.get_attribute(name)
            except Exception:
                value = None
            if name == "disabled" and value is not None:
                return True
            if str(value or "").lower() in {"true", "disabled"} or "disabled" in str(value or "").lower().split():
                return True
        return False

    @staticmethod
    def _page_signature(page) -> str:
        try:
            soup = BeautifulSoup(page.content(), "html.parser")
        except Exception:
            return ""
        values = []
        for anchor in soup.find_all("a", href=True):
            href = html_lib.unescape(anchor.get("href", ""))
            if "/position/" in href and "/detail" in href:
                values.append(href)
                if len(values) >= 3:
                    break
        return "|".join(values)

    def pagination_metrics(self) -> dict:
        return {
            "pages_seen": self.pages_seen,
            "advertised_total": self.advertised_total,
            "total_pages": self.total_pages,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "resolved_source_url": self.resolved_source_url,
            "fetch_failed": self.fetch_failed,
            "evidence": list(self.pagination_evidence),
        }

    @staticmethod
    def _remaining_ms(deadline: float, fallback: int) -> int:
        return max(1, min(fallback, int((deadline - time.monotonic()) * 1000)))

    def fetch(self) -> list[dict]:
        self._reset_pagination_state()
        try:
            from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
        except ImportError:
            logger.error("[%s] 未安装 playwright", self.company_name)
            self.fetch_failed = True
            self.pagination_termination_reason = "playwright_unavailable"
            return []

        deadline = time.monotonic() + effective_crawl_timeout_seconds(
            self.HARD_TIMEOUT_MS / 1000
        )
        candidates = []
        for value in [self.LIST_URL, *self._candidate_list_urls(self.careers_url)]:
            value = self._clean_url(value)
            if value and value not in candidates:
                candidates.append(value)
        if not candidates:
            self.pagination_termination_reason = "job_list_route_not_found"
            return []

        all_jobs: list[dict] = []
        seen_urls: set[str] = set()
        accepted_page = None
        api_facts: dict[tuple[int, int], dict] = {}
        api_request: dict = {}
        current_list_url = ""
        browser = None
        context = None

        def close_browser() -> None:
            nonlocal browser, context
            if context is not None:
                try:
                    context.close()
                finally:
                    context = None
            if browser is not None:
                try:
                    browser.close()
                finally:
                    browser = None

        try:
            with sync_playwright() as playwright:
                browser = launch_browser(
                    playwright,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                    locale="zh-CN",
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                    else route.continue_(),
                )

                def capture_search(response) -> None:
                    if response.request.method.upper() != "POST":
                        return
                    if urlsplit(response.url).path.rstrip("/") != self.API_PATH:
                        return
                    if response.status != 200:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    try:
                        request_payload = response.request.post_data_json
                    except Exception:
                        request_payload = None
                    parsed = self._parse_api_payload(
                        payload,
                        current_list_url,
                        response.url,
                        request_payload,
                    )
                    if parsed is not None:
                        api_facts[(parsed["offset"], parsed["limit"])] = parsed
                        if not api_request:
                            api_request.update(
                                url=response.url,
                                headers=dict(response.request.headers),
                                payload=request_payload or {},
                            )

                page.on("response", capture_search)

                def fetch_api_page(offset: int, limit: int) -> dict | None:
                    if not api_request:
                        return None
                    payload = dict(api_request.get("payload") or {})
                    payload.update(offset=offset, limit=limit)
                    headers = {
                        key: value
                        for key, value in (api_request.get("headers") or {}).items()
                        if key.lower() in {
                            "accept", "accept-language", "content-type", "x-csrf-token",
                            "portal-channel", "portal-platform", "website-path",
                        }
                    }
                    api_url = self._api_page_url(api_request["url"], offset, limit)
                    for attempt in range(3):
                        if time.monotonic() >= deadline:
                            return None
                        try:
                            result = page.evaluate(
                                """async ({url, headers, payload, timeoutMs}) => {
                                    const controller = new AbortController();
                                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                                    try {
                                        const response = await fetch(url, {
                                            method: 'POST',
                                            headers,
                                            body: JSON.stringify(payload),
                                            credentials: 'include',
                                            signal: controller.signal
                                        });
                                        return {status: response.status, text: await response.text()};
                                    } finally {
                                        clearTimeout(timer);
                                    }
                                }""",
                                {
                                    "url": api_url,
                                    "headers": headers,
                                    "payload": payload,
                                    "timeoutMs": self._remaining_ms(deadline, 10_000),
                                },
                            )
                            if isinstance(result, dict) and result.get("status") == 200:
                                response_payload = json.loads(result.get("text") or "")
                                parsed = self._parse_api_payload(
                                    response_payload,
                                    current_list_url,
                                    api_url,
                                    payload,
                                )
                                if parsed is not None:
                                    return parsed
                        except Exception as exc:
                            logger.debug(
                                "[%s] 飞书 API offset=%d 第 %d 次重试: %s",
                                self.company_name,
                                offset,
                                attempt + 1,
                                exc,
                            )
                        if attempt < 2:
                            page.wait_for_timeout(self._remaining_ms(deadline, 250 * (attempt + 1)))
                    return None

                try:
                    for candidate in candidates:
                        if time.monotonic() >= deadline:
                            break
                        current_list_url = candidate
                        api_facts.clear()
                        api_request.clear()
                        try:
                            page.goto(
                                candidate,
                                wait_until="domcontentloaded",
                                timeout=self._remaining_ms(deadline, self.GOTO_TIMEOUT_MS),
                            )
                        except PWTimeout as exc:
                            logger.warning("[%s] 飞书入口超时 %s: %s", self.company_name, candidate, exc)
                        try:
                            page.wait_for_selector(
                                ".positionItem-title-text, .listNoData-text",
                                timeout=self._remaining_ms(deadline, 30000),
                            )
                        except PWTimeout:
                            pass
                        page.wait_for_timeout(self._remaining_ms(deadline, 5000))
                        html = page.content()
                        candidates.extend(
                            value for value in self._candidate_list_urls(candidate, html)
                            if value not in candidates
                        )
                        dom_jobs = self._parse_page_jobs(page)
                        notoken = "/notoken" in (page.url or candidate).lower()
                        if api_facts or dom_jobs or (self._advertised_total_from_text(html) == 0 and not notoken):
                            if not notoken or api_facts or dom_jobs:
                                accepted_page = page
                                self.resolved_source_url = page.url or candidate
                                break
                except Exception:
                    raise

                if accepted_page is None:
                    self.pagination_complete = False
                    self.pagination_termination_reason = (
                        "hard_timeout" if time.monotonic() >= deadline else "job_list_route_not_found"
                    )
                    self.fetch_failed = time.monotonic() >= deadline
                    close_browser()
                    return []

                page_number = 0
                processed_offsets: set[tuple[int, int]] = set()
                api_collected_count = 0
                dom_fallback_mode = not bool(api_facts)
                while page_number < self.MAX_PAGES:
                    if time.monotonic() >= deadline:
                        self.pagination_complete = False
                        self.has_more = self.advertised_total is None or api_collected_count < self.advertised_total
                        self.pagination_termination_reason = "hard_timeout"
                        break
                    page_number += 1
                    self.pages_seen = max(self.pages_seen, page_number)
                    self.pages_fetched = self.pages_seen
                    page.wait_for_timeout(self._remaining_ms(deadline, 1200))

                    for key, fact in sorted(api_facts.items()):
                        if key in processed_offsets:
                            continue
                        processed_offsets.add(key)
                        api_collected_count += fact["raw_count"]
                        self.advertised_total = fact["total"]
                        self.expected_total = fact["total"]
                        if fact["total"] is not None and fact["limit"]:
                            self.total_pages = math.ceil(fact["total"] / fact["limit"])
                        self.has_more = bool(fact["has_more"])
                        self.pagination_evidence.append({
                            "page": page_number,
                            "offset": fact["offset"],
                            "limit": fact["limit"],
                            "rows": fact["raw_count"],
                            "collected": api_collected_count,
                            "advertised_total": fact["total"],
                            "has_more": fact["has_more"],
                            "explicit_has_more": fact["explicit_has_more"],
                        })
                        for job in fact["jobs"]:
                            if job["jd_url"] not in seen_urls:
                                seen_urls.add(job["jd_url"])
                                all_jobs.append(job)

                    dom_jobs = self._parse_page_jobs(page)
                    if dom_fallback_mode or not all_jobs:
                        for job in dom_jobs:
                            if job["jd_url"] not in seen_urls:
                                seen_urls.add(job["jd_url"])
                                all_jobs.append(job)
                    if self.advertised_total is None:
                        self.advertised_total = self._advertised_total_from_text(page.content())
                        self.expected_total = self.advertised_total

                    if self.advertised_total is not None:
                        if not self.has_more and (
                            api_collected_count >= self.advertised_total or not api_facts
                        ):
                            self.pagination_complete = True
                            self.has_more = False
                            self.pagination_termination_reason = "api_has_more_false" if api_facts else "total_reached"
                            break
                        self.has_more = api_collected_count < self.advertised_total
                    elif not api_facts:
                        self.has_more = True

                    if api_facts and api_request and self.has_more:
                        last_fact = max(api_facts.values(), key=lambda fact: fact["offset"])
                        next_offset = last_fact["offset"] + last_fact["limit"]
                        next_fact = fetch_api_page(next_offset, last_fact["limit"])
                        if next_fact is not None:
                            api_facts[(next_fact["offset"], next_fact["limit"])] = next_fact
                            if not next_fact["raw_count"] and next_fact["has_more"]:
                                self.pagination_complete = False
                                self.pagination_termination_reason = "api_empty_before_total"
                                self.has_more = True
                                break
                            continue

                    next_btn = self._next_button(page)
                    if next_btn is None:
                        self.pagination_complete = not self.has_more
                        self.pagination_termination_reason = (
                            "terminal_without_total" if self.pagination_complete
                            else "api_has_more_without_pagination"
                        )
                        break
                    if self._button_disabled(next_btn):
                        self.pagination_complete = not self.has_more
                        self.pagination_termination_reason = (
                            "terminal_button_disabled" if self.pagination_complete
                            else "terminal_before_total"
                        )
                        break

                    before_offsets = set(api_facts)
                    before_signature = self._page_signature(page)
                    try:
                        next_btn.click()
                    except Exception as exc:
                        self.pagination_complete = False
                        self.pagination_termination_reason = "next_page_error"
                        self.has_more = True
                        logger.warning("[%s] 飞书翻页失败: %s", self.company_name, exc)
                        break

                    progressed = False
                    for _ in range(8):
                        page.wait_for_timeout(self._remaining_ms(deadline, 1000))
                        if set(api_facts) - before_offsets:
                            progressed = True
                            break
                        if self._page_signature(page) and self._page_signature(page) != before_signature:
                            progressed = True
                            break
                    if not progressed:
                        self.pagination_complete = False
                        self.has_more = True
                        self.pagination_termination_reason = "page_stalled"
                        break
                else:
                    self.pagination_complete = False
                    self.has_more = True
                    self.pagination_termination_reason = "safety_limit"
                close_browser()
        except Exception as exc:
            logger.error("[%s] 飞书岗位抓取异常: %s", self.company_name, exc)
            self.fetch_failed = True
            self.pagination_complete = False
            if self.pagination_termination_reason == "not_started":
                self.pagination_termination_reason = "fetch_failed"
        finally:
            close_browser()

        logger.info(
            "[%s] 飞书抓到 %d 个岗位（预期 %s）",
            self.company_name,
            len(all_jobs),
            self.advertised_total if self.advertised_total is not None else "未知",
        )
        return all_jobs


class GenericFeishuCrawler(FeishuRecruitCrawler):
    """通用飞书招聘爬虫：从 careers_url 自动推导 LIST_URL/HOST，服务任意飞书租户。

    各租户路径 token 不同（campus / campusrecruitment / ponycampus / 398875 …），
    但 DOM（.positionItem-title-text）和详情锚点（/<token>/position/<id>/detail）一致，
    故基类只认 "/position/" + "/detail" 即可通用。

    config 用法：crawler: feishu + careers_url（岗位列表页或申请页都行，自动去 /application）。
    """

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        cleaned_url = self._clean_url(careers_url)
        p = urlparse(cleaned_url)
        self.HOST = f"{p.scheme}://{p.netloc}"
        candidates = self._candidate_list_urls(cleaned_url)
        self.LIST_URL = candidates[0] if candidates else cleaned_url
        self.GOTO_WAIT_UNTIL = "domcontentloaded"
