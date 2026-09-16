"""Huawei campus recruitment adapter.

Huawei's current campus page is a server-rendered shell whose job list is
loaded by a browser-only API request.  The adapter keeps the browser session
read-only, records the API's page metadata, and uses the public
``advertisementId`` for stable detail links.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from .base import BaseCrawler, launch_browser

logger = logging.getLogger(__name__)


class HuaweiCrawler(BaseCrawler):
    """Crawl the shared official Huawei campus recruitment list.

    Department/project names from an OC lead are deliberately ignored when
    building the request.  Every supported ``career.huawei.com`` entry is
    canonicalized to the same official list, so aliases cannot accidentally
    become different department-filtered crawls.
    """

    OFFICIAL_HOST = "career.huawei.com"
    LIST_PATH = "/cn/campus-recruitment-job-list"
    DETAIL_PATH = "/cn/job-details"
    DEFAULT_RECRUITMENT_TYPE = "FRESH_GRADUATE"
    RECRUITMENT_TYPES = frozenset({"FRESH_GRADUATE", "INTERN"})

    API_HOST = "apigw-dgg-b0.huawei.com"
    API_PATH = "/api/apig/channelhw/recruitmentPosition/pub/getJobPage"
    API_PATH_FRAGMENT = "/recruitmentPosition/pub/getJobPage"
    APP_ID = "app_000000035886"
    TENANT_ALIAS = "hcm"
    LANGUAGE = "zh_CN"

    PAGE_SIZE = 10
    MAX_PAGES = 1000
    NAVIGATION_TIMEOUT_MS = 60_000
    RESPONSE_TIMEOUT_MS = 30_000
    JD_RAW_LIMIT = 12_000
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    LIST_URL = (
        "https://career.huawei.com/cn/campus-recruitment-job-list"
        "?recruitmentType=FRESH_GRADUATE"
    )
    DETAIL_URL_TEMPLATE = (
        "https://career.huawei.com/cn/job-details?advertisementId={ad_id}"
    )

    _ENTRY_PATHS = frozenset(
        {
            "/cn",
            "/cn/",
            "/cn/campus-recruitment",
            "/cn/campus-recruitment/",
            LIST_PATH,
            "/reccampportal/portal5/campus-recruitment.html",
        }
    )

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.recruitment_type = self._recruitment_type(careers_url)
        self.resolved_source_url = self._canonical_list_url(careers_url)
        self._reset_evidence()

    @classmethod
    def _recruitment_type(cls, url: str) -> str:
        values = parse_qs(urlsplit(url or "").query).get("recruitmentType") or []
        value = str(values[0] or "").strip().upper() if values else ""
        return value if value in cls.RECRUITMENT_TYPES else cls.DEFAULT_RECRUITMENT_TYPE

    @classmethod
    def _canonical_list_url(cls, url: str, recruitment_type: str | None = None) -> str:
        """Return one list identity and drop department/project query noise."""
        selected = (recruitment_type or cls._recruitment_type(url)).upper()
        if selected not in cls.RECRUITMENT_TYPES:
            selected = cls.DEFAULT_RECRUITMENT_TYPE
        return urlunsplit(
            (
                "https",
                cls.OFFICIAL_HOST,
                cls.LIST_PATH,
                urlencode({"recruitmentType": selected}),
                "",
            )
        )

    canonical_source_url = _canonical_list_url

    @classmethod
    def _is_supported_entry_url(cls, url: str) -> bool:
        parsed = urlsplit(url or "")
        host = (parsed.hostname or "").casefold().rstrip(".")
        path = parsed.path.rstrip("/").casefold() or "/"
        if parsed.scheme.casefold() not in {"http", "https"} or host != cls.OFFICIAL_HOST:
            return False
        if path in {item.rstrip("/").casefold() for item in cls._ENTRY_PATHS}:
            return True
        return "campus-recruitment" in path and "social-recruitment" not in path

    @classmethod
    def _detail_url(cls, advertisement_id: object) -> str:
        value = quote(str(advertisement_id).strip(), safe="")
        return f"https://{cls.OFFICIAL_HOST}{cls.DETAIL_PATH}?advertisementId={value}"

    def _reset_evidence(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.pages_fetched = 0
        self.total_pages: int | None = None
        self.expected_pages: int | None = None
        self.page_size: int | None = None
        self.advertised_total: int | None = None
        self.expected_total: int | None = None
        self.has_more = False
        self.fetch_failed = False
        self.page_sizes: list[int] = []
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.pagination_duplicate_ids: list[str] = []
        self.invalid_row_count = 0
        self.completeness_evidence: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}
        self._update_evidence()

    @staticmethod
    def _as_int(value: object) -> int | None:
        try:
            if value in (None, ""):
                return None
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _clean_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            value = " / ".join(str(item) for item in value if str(item).strip())
        text = html.unescape(str(value))
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "\n", text)
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    @classmethod
    def _parse_page_payload(
        cls,
        payload: object,
        requested_page: int | None = None,
    ) -> dict[str, Any] | None:
        """Normalize one ``getJobPage`` response without guessing totals."""
        if not isinstance(payload, Mapping):
            return None
        status = str(payload.get("status") or "").strip().upper()
        if status and status not in {"SUCCESS", "OK"}:
            return None
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None
        rows = data.get("result")
        page_vo = data.get("pageVO")
        if not isinstance(rows, list) or not isinstance(page_vo, Mapping):
            return None

        page = cls._as_int(page_vo.get("curPage")) or cls._as_int(requested_page)
        page_size = cls._as_int(page_vo.get("pageSize"))
        total = cls._as_int(page_vo.get("totalRows"))
        total_pages = cls._as_int(page_vo.get("totalPages"))
        if page is None or page < 1 or page_size is None or page_size < 1 or total is None:
            return None
        if total_pages is None:
            total_pages = max(1, math.ceil(total / page_size))
        elif total_pages < 1:
            total_pages = 1

        valid_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_more": page < total_pages,
            "rows": valid_rows,
            "raw_row_count": len(rows),
            "invalid_row_count": len(rows) - len(valid_rows),
        }

    @classmethod
    def _request_page(cls, post_data: object) -> int | None:
        if isinstance(post_data, Mapping):
            return cls._as_int(post_data.get("curPage"))
        if not post_data:
            return None
        try:
            payload = json.loads(str(post_data))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return cls._request_page(payload)

    @classmethod
    def _is_api_response(cls, response: Any) -> bool:
        url = str(getattr(response, "url", "") or "")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        return host.endswith(".huawei.com") and cls.API_PATH_FRAGMENT in parsed.path

    def _capture_response(self, response: Any, pages: dict[int, dict[str, Any]]) -> None:
        if not self._is_api_response(response) or getattr(response, "status", 0) != 200:
            return
        request = getattr(response, "request", None)
        requested_page = self._request_page(getattr(request, "post_data", None))
        try:
            parsed = self._parse_page_payload(response.json(), requested_page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 华为岗位 API JSON 解析失败: %s", self.company_name, exc)
            return
        if parsed is None:
            logger.warning("[%s] 华为岗位 API 返回缺少分页元数据", self.company_name)
            return
        page = int(parsed["page"])
        # A late duplicate response may arrive after a retry.  Keep the larger
        # page payload so a transient empty response cannot erase observations.
        previous = pages.get(page)
        if previous is None or len(parsed["rows"]) >= len(previous["rows"]):
            pages[page] = parsed

    @classmethod
    def _response_for_page(cls, response: Any, page: int) -> bool:
        if not cls._is_api_response(response):
            return False
        request = getattr(response, "request", None)
        return cls._request_page(getattr(request, "post_data", None)) == page

    @staticmethod
    def _navigate_to_page(page: Any, target_page: int) -> bool:
        """Click an exact visible number, otherwise the pager's next button."""
        pager = page.locator(".aui-pager").first
        if pager.count() == 0:
            raise RuntimeError("pagination_control_not_found")

        items = pager.locator("li")
        target_text = str(target_page)
        for index in range(items.count()):
            item = items.nth(index)
            if item.inner_text().strip() != target_text:
                continue
            classes = item.get_attribute("class") or ""
            if "is-active" in classes:
                return False
            item.click()
            return True

        buttons = pager.locator("button")
        if buttons.count() < 2:
            raise RuntimeError("next_page_control_not_found")
        next_button = buttons.nth(buttons.count() - 1)
        if next_button.is_disabled():
            raise RuntimeError("next_page_control_disabled")
        next_button.click()
        return True

    def _browser_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        proxy = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        return kwargs

    def _fetch_pages(self) -> dict[int, dict[str, Any]]:
        """Use the page's own browser context to observe every list response."""
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.fetch_failed = True
            self.pagination_termination_reason = "playwright_unavailable"
            return {}

        pages: dict[int, dict[str, Any]] = {}
        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                browser = launch_browser(playwright, **self._browser_kwargs())
                context = browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                    locale="zh-CN",
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "media", "font"}
                    else route.continue_(),
                )
                page.on("response", lambda response: self._capture_response(response, pages))

                try:
                    with page.expect_response(
                        lambda response: self._response_for_page(response, 1),
                        timeout=self.RESPONSE_TIMEOUT_MS,
                    ) as first_response:
                        page.goto(
                            self.resolved_source_url,
                            wait_until="domcontentloaded",
                            timeout=self.NAVIGATION_TIMEOUT_MS,
                        )
                    self._capture_response(first_response.value, pages)
                except PWTimeout:
                    logger.warning("[%s] 华为第 1 页 API 响应超时", self.company_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] 华为列表页加载失败: %s", self.company_name, exc)

                if 1 not in pages:
                    self.fetch_failed = True
                    self.pagination_termination_reason = "first_page_api_missing"
                    return pages

                expected_pages = int(pages[1]["total_pages"])
                if expected_pages > self.MAX_PAGES:
                    self.pagination_termination_reason = "max_pages"
                    return pages

                for target_page in range(2, expected_pages + 1):
                    if target_page in pages:
                        continue
                    try:
                        with page.expect_response(
                            lambda response, target=target_page: self._response_for_page(
                                response, target
                            ),
                            timeout=self.RESPONSE_TIMEOUT_MS,
                        ) as next_response:
                            clicked = self._navigate_to_page(page, target_page)
                        if clicked:
                            self._capture_response(next_response.value, pages)
                        if target_page not in pages:
                            self.pagination_termination_reason = (
                                f"page_{target_page}_api_missing"
                            )
                            break
                    except PWTimeout:
                        self.pagination_termination_reason = f"page_{target_page}_api_timeout"
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.pagination_termination_reason = (
                            f"page_{target_page}_navigation_failed"
                        )
                        logger.warning(
                            "[%s] 华为第 %d 页翻页失败: %s",
                            self.company_name,
                            target_page,
                            exc,
                        )
                        break
        except Exception as exc:  # noqa: BLE001
            self.fetch_failed = True
            self.pagination_termination_reason = "browser_session_failed"
            logger.error("[%s] 华为爬取异常: %s", self.company_name, exc)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[%s] 华为浏览器上下文关闭失败", self.company_name)
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[%s] 华为浏览器关闭失败", self.company_name)
        return pages

    def _compose_jd(self, item: Mapping[str, Any]) -> str:
        duties = self._clean_text(item.get("mainBusiness"))
        requirements = self._clean_text(item.get("jobRequire"))
        parts: list[str] = []
        if duties:
            parts.extend(["岗位职责", duties])
        if requirements:
            parts.extend(["任职要求", requirements])
        return "\n".join(parts)[: self.JD_RAW_LIMIT]

    def _normalize_job(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        advertisement_id = self._clean_text(item.get("advertisementId"))
        title = self._clean_text(item.get("jobName") or item.get("jobNameNew"))
        if not advertisement_id or not title:
            return None

        city = next(
            (
                self._clean_text(item.get(field))
                for field in (
                    "workPlace",
                    "workArea",
                    "jobArea",
                    "countryName",
                    "cityName",
                    "jobAddress",
                )
                if self._clean_text(item.get(field))
            ),
            "",
        )
        scenario = self._clean_text(item.get("scenarioName"))
        job_type = "实习" if "实习" in scenario or self.recruitment_type == "INTERN" else "校招"
        job = self._make_job(
            title=title,
            city=city,
            job_type=job_type,
            jd_url=self._detail_url(advertisement_id),
            jd_raw=self._compose_jd(item),
            published_at=self._clean_text(
                item.get("lastUpdateDate")
                or item.get("releaseDate")
                or item.get("deployDate")
            ),
        )
        job.update(
            {
                "source_job_id": advertisement_id,
                "source_advertisement_id": advertisement_id,
                "source_list_url": self.resolved_source_url,
                "official_source": "career.huawei.com",
            }
        )
        return job

    def _update_evidence(self) -> None:
        pagination = {
            "source_url": self.careers_url,
            "effective_source_url": self.resolved_source_url,
            "api_url": f"https://{self.API_HOST}{self.API_PATH}?X-HW-ID={self.APP_ID}",
            "recruitment_type": self.recruitment_type,
            "pages_seen": self.pages_seen,
            "total_pages": self.total_pages,
            "page_size": self.page_size,
            "page_sizes": list(self.page_sizes),
            "advertised_total": self.advertised_total,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "duplicate_ids": list(self.pagination_duplicate_ids),
            "invalid_row_count": self.invalid_row_count,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
        }
        self.completeness_evidence = dict(pagination)
        self.metrics = {
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
            "pagination": pagination,
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return dict(self.metrics.get("pagination") or {})

    def _finalize_pages(self, pages: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
        ordered_pages = sorted((int(page), payload) for page, payload in pages.items())
        self.pages_seen = len(ordered_pages)
        self.pages_fetched = self.pages_seen
        self.page_sizes = [len(payload.get("rows") or []) for _, payload in ordered_pages]
        self.raw_listed_count = sum(
            int(payload.get("raw_row_count") or len(payload.get("rows") or []))
            for _, payload in ordered_pages
        )
        self.invalid_row_count = sum(
            int(payload.get("invalid_row_count") or 0) for _, payload in ordered_pages
        )

        if not ordered_pages:
            if self.pagination_termination_reason == "not_started":
                self.pagination_termination_reason = "no_api_pages"
            self.fetch_failed = True
            self._update_evidence()
            return []

        first_payload = ordered_pages[0][1]
        self.advertised_total = self._as_int(first_payload.get("total"))
        self.expected_total = self.advertised_total
        self.total_pages = self._as_int(first_payload.get("total_pages"))
        self.expected_pages = self.total_pages
        self.page_size = self._as_int(first_payload.get("page_size"))
        metadata_consistent = True
        for _, payload in ordered_pages:
            if (
                self._as_int(payload.get("total")) != self.advertised_total
                or self._as_int(payload.get("total_pages")) != self.total_pages
                or self._as_int(payload.get("page_size")) != self.page_size
            ):
                metadata_consistent = False
                break

        seen_ids: set[str] = set()
        jobs: list[dict[str, Any]] = []
        for _, payload in ordered_pages:
            for item in payload.get("rows") or []:
                advertisement_id = self._clean_text(item.get("advertisementId"))
                if not advertisement_id:
                    continue
                if advertisement_id in seen_ids:
                    self.pagination_duplicate_ids.append(advertisement_id)
                    continue
                seen_ids.add(advertisement_id)
                job = self._normalize_job(item)
                if job is not None:
                    jobs.append(job)
        self.unique_listed_count = len(seen_ids)

        expected_pages = self.total_pages or 0
        observed_page_numbers = {page for page, _ in ordered_pages}
        complete_pages = (
            expected_pages > 0
            and observed_page_numbers == set(range(1, expected_pages + 1))
        )
        self.has_more = bool(expected_pages and self.pages_seen < expected_pages)
        if any(bool(payload.get("has_more")) for _, payload in ordered_pages):
            self.has_more = self.has_more or not complete_pages

        complete_rows = (
            self.advertised_total is not None
            and self.raw_listed_count == self.advertised_total
            and self.unique_listed_count == self.advertised_total
            and len(jobs) == self.advertised_total
        )
        self.pagination_complete = bool(
            metadata_consistent
            and complete_pages
            and complete_rows
            and self.invalid_row_count == 0
            and not self.pagination_duplicate_ids
            and not self.fetch_failed
        )
        if self.pagination_complete:
            self.has_more = False
            self.pagination_termination_reason = "api_total_and_pages_reached"
        elif self.pagination_termination_reason == "not_started":
            if not metadata_consistent:
                self.pagination_termination_reason = "advertised_total_changed"
            elif self.invalid_row_count:
                self.pagination_termination_reason = "invalid_api_row"
            elif self.pagination_duplicate_ids:
                self.pagination_termination_reason = "duplicate_advertisement_id"
            elif not complete_pages:
                missing = sorted(set(range(1, expected_pages + 1)) - observed_page_numbers)
                self.pagination_termination_reason = (
                    f"missing_api_page_{missing[0]}" if missing else "page_count_mismatch"
                )
            else:
                self.pagination_termination_reason = "advertised_total_mismatch"
        self._update_evidence()
        return jobs

    def fetch(self) -> list[dict]:
        self._reset_evidence()
        if not self._is_supported_entry_url(self.careers_url):
            self.pagination_termination_reason = "non_official_or_wrong_campus_url"
            self._update_evidence()
            return []

        pages = self._fetch_pages()
        return self._finalize_pages(pages)


__all__ = ["HuaweiCrawler"]
