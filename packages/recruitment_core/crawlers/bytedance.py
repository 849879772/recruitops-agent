import logging
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..api_capture import (
    OFFICIAL_BYTEDANCE_API_PATH,
    OfficialApiResponseContext,
    build_official_api_capture_evidence,
    native_post_id,
    normalize_official_jd,
)
from .feishu import FeishuRecruitCrawler
from .base import launch_browser

logger = logging.getLogger(__name__)


class ByteDanceCrawler(FeishuRecruitCrawler):
    """字节跳动校招：通过官网岗位 API 按总数完整分页。"""

    LIST_URL = "https://jobs.bytedance.com/campus/position"
    HOST = "https://jobs.bytedance.com"
    GOTO_TIMEOUT_MS = 45000
    API_PAGE_SIZE = 100
    FORMAL_CAMPAIGNS = (
        "2027届校园招聘",
        "2027届前沿技术领域人才校招",
        "2027届Seed大模型人才校招",
    )

    @classmethod
    def _is_formal_campaign_payload(cls, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        subject_ids = payload.get("subject_id_list") or []
        return (
            isinstance(subject_ids, list)
            and len(subject_ids) == len(cls.FORMAL_CAMPAIGNS)
            and all(str(subject_id).strip() for subject_id in subject_ids)
        )

    @classmethod
    def _select_formal_campaigns(cls, page) -> None:
        """Select the three current full-time campus projects in the portal tree."""

        for label in cls.FORMAL_CAMPAIGNS:
            option = page.get_by_text(label, exact=True).first
            option.wait_for(state="visible", timeout=15000)
            option.evaluate(
                """node => {
                    const treeItem = node.closest("li");
                    const checkbox = treeItem && treeItem.querySelector(".atsx-tree-checkbox");
                    if (!checkbox) throw new Error(`missing campaign checkbox: ${node.textContent}`);
                    checkbox.click();
                }"""
            )
            page.wait_for_timeout(1200)

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.total_count = 0
        self.advertised_total = None
        self.expected_total = None
        self.pages_seen = 0
        self.total_pages = None
        self.has_more = False
        self.fetch_failed = False
        self.pagination_evidence: list[dict] = []
        jobs_by_id: dict[str, dict] = {}

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("[%s] 未安装 playwright", self.company_name)
            self.fetch_failed = True
            self.pagination_termination_reason = "playwright_unavailable"
            return list(jobs_by_id.values())

        try:
            with sync_playwright() as playwright:
                browser = launch_browser(
                    playwright,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                browser_context = browser.new_context(locale="zh-CN")
                page = browser_context.new_page()
                captured: dict = {}

                def capture_search(response):
                    if (
                        response.request.method.upper() != "POST"
                        or urlsplit(response.url).path.rstrip("/")
                        != OFFICIAL_BYTEDANCE_API_PATH.rstrip("/")
                        or response.status != 200
                    ):
                        return
                    try:
                        payload = response.request.post_data_json
                    except Exception:
                        return
                    if self._is_formal_campaign_payload(payload):
                        captured.clear()
                        captured.update(
                            url=response.url,
                            method=response.request.method.upper(),
                            headers=response.request.headers,
                            payload=payload,
                        )

                page.on("response", capture_search)
                page.goto(
                    self.LIST_URL,
                    wait_until="domcontentloaded",
                    timeout=self.GOTO_TIMEOUT_MS,
                )
                page.wait_for_selector(
                    ".positionItem-title-text",
                    timeout=30000,
                )
                self._select_formal_campaigns(page)
                for _ in range(10):
                    if captured:
                        break
                    page.wait_for_timeout(500)
                if not captured:
                    raise RuntimeError("未捕获到字节 2027 正式校招岗位搜索 API")

                csrf = captured["headers"].get("x-csrf-token", "")
                base_payload = dict(captured["payload"])
                for offset in range(0, 1_000_000, self.API_PAGE_SIZE):
                    payload = dict(base_payload)
                    payload.update(limit=self.API_PAGE_SIZE, offset=offset)
                    api_url = self._api_page_url(
                        captured["url"], offset, self.API_PAGE_SIZE
                    )
                    result = None
                    for attempt in range(3):
                        result = page.evaluate(
                            """async ({url, payload, csrf}) => {
                                const response = await fetch(url, {
                                    method: "POST",
                                    headers: {
                                        "content-type": "application/json",
                                        "x-csrf-token": csrf,
                                        "portal-channel": "campus",
                                        "portal-platform": "pc",
                                        "website-path": "campus"
                                    },
                                    body: JSON.stringify(payload),
                                    credentials: "include"
                                });
                                return {
                                    status: response.status,
                                    text: await response.text()
                                };
                            }""",
                            {"url": api_url, "payload": payload, "csrf": csrf},
                        )
                        if (
                            isinstance(result, dict)
                            and result.get("status") == 200
                            and result.get("text")
                        ):
                            break
                        page.wait_for_timeout(1000 * (attempt + 1))
                    if (
                        not isinstance(result, dict)
                        or result.get("status") != 200
                        or not result.get("text")
                    ):
                        raise RuntimeError(
                            f"岗位 API 分页失败 offset={offset}, "
                            f"status={result and result.get('status')}"
                        )

                    response_context = OfficialApiResponseContext.from_captured_response(
                        response_url=api_url,
                        request_method=captured.get("method", "POST"),
                        response_status=result.get("status"),
                        request_payload=payload,
                        response_text=result.get("text") or "",
                    )
                    parsed = self._parse_api_page(
                        response_context,
                        self.LIST_URL,
                        payload,
                    )
                    if parsed is None:
                        raise RuntimeError(
                            f"岗位 API 返回未知或不完整响应 offset={offset}"
                        )
                    items = parsed["items"]
                    self.total_count = parsed["total"]
                    self.advertised_total = self.total_count
                    self.expected_total = self.total_count
                    self.pages_seen += 1
                    self.total_pages = (
                        (self.total_count + parsed["limit"] - 1) // parsed["limit"]
                        if parsed["limit"]
                        else None
                    )
                    for job_id, job in parsed["jobs_by_id"].items():
                        jobs_by_id[job_id] = job
                    self.has_more = parsed["has_more"]
                    self.pagination_evidence.append(
                        {
                            "offset": parsed["offset"],
                            "limit": parsed["limit"],
                            "rows": len(items),
                            "collected": len(jobs_by_id),
                            "advertised_total": self.total_count,
                            "has_more": self.has_more,
                            "response_sha256": response_context.raw_response_sha256,
                        }
                    )
                    logger.info(
                        "[%s] API offset=%d 返回 %d 条，累计 %d/%d",
                        self.company_name,
                        offset,
                        len(items),
                        len(jobs_by_id),
                        self.total_count,
                    )
                    if offset + len(items) >= self.total_count:
                        self.pagination_complete = len(jobs_by_id) == self.total_count
                        self.pagination_termination_reason = (
                            "api_total_reached"
                            if self.pagination_complete
                            else "api_count_mismatch"
                        )
                        self.has_more = False
                        break
                    if not items:
                        self.has_more = True
                        self.pagination_termination_reason = "api_empty_before_total"
                        break

                if (
                    not self.pagination_complete
                    and self.pagination_termination_reason == "not_started"
                ):
                    self.has_more = True
                    self.pagination_termination_reason = "api_page_limit"

                browser_context.close()
                browser.close()
        except Exception as exc:
            self.fetch_failed = True
            if self.pages_seen or jobs_by_id:
                self.has_more = True
            if self.pagination_termination_reason == "not_started":
                self.pagination_termination_reason = "api_request_failed"
            logger.error("[%s] 字节岗位 API 抓取异常: %s", self.company_name, exc)

        if not self.pagination_complete:
            logger.error(
                "[%s] API 抓取未完整结束：%s，获得 %d/%d",
                self.company_name,
                self.pagination_termination_reason,
                len(jobs_by_id),
                self.total_count,
            )
        return list(jobs_by_id.values())

    def _parse_api_page(
        self,
        response_context: OfficialApiResponseContext | None,
        list_url: str,
        request_payload: dict,
    ) -> dict | None:
        """Parse one captured page while retaining its raw response identity."""

        if not isinstance(response_context, OfficialApiResponseContext):
            return None
        if not response_context.verified:
            return None
        payload = response_context.response_payload
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        items = data.get("job_post_list")
        if not isinstance(items, list):
            return None
        total = self._json_int(data.get("count"))
        if total is None:
            return None

        query = dict(parse_qsl(urlsplit(response_context.response_url).query))
        offset = self._json_int(request_payload.get("offset"))
        limit = self._json_int(request_payload.get("limit"))
        if offset is None:
            offset = self._json_int(query.get("offset"))
        if limit is None:
            limit = self._json_int(query.get("limit"))
        offset = offset if offset is not None else 0
        limit = limit if limit is not None else (len(items) or self.API_PAGE_SIZE)

        explicit_has_more = data.get("has_more")
        if explicit_has_more is None:
            explicit_has_more = data.get("hasMore")
        if explicit_has_more is not None and not isinstance(explicit_has_more, (bool, int)):
            return None
        has_more = (
            bool(explicit_has_more)
            if explicit_has_more is not None
            else offset + len(items) < total
        )

        jobs_by_id: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            job_id = self._native_post_id(item)
            if not job_id:
                continue
            jobs_by_id[job_id] = self._parse_api_job(
                item,
                response_context=response_context,
            )
        return {
            "items": items,
            "jobs_by_id": jobs_by_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "list_url": list_url,
        }

    @staticmethod
    def _api_page_url(url: str, offset: int, limit: int) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(offset=str(offset), limit=str(limit))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    @staticmethod
    def _native_post_id(item: dict) -> str:
        return native_post_id(item.get("id"))

    def _parse_api_job(
        self,
        item: dict,
        *,
        response_context: OfficialApiResponseContext | None = None,
        api_context: OfficialApiResponseContext | None = None,
    ) -> dict:
        if response_context is None:
            response_context = api_context
        job_id = self._native_post_id(item)
        city_list = item.get("city_list") or []
        cities = [
            str(city.get("name") or city.get("i18n_name") or "").strip()
            for city in city_list
            if isinstance(city, dict)
        ]
        if not cities and isinstance(item.get("city_info"), dict):
            cities = [
                str(
                    item["city_info"].get("name")
                    or item["city_info"].get("i18n_name")
                    or ""
                ).strip()
            ]

        recruit_type = item.get("recruit_type") or {}
        subject = item.get("job_subject") or {}
        subject_name = subject.get("name") if isinstance(subject, dict) else ""
        if isinstance(subject_name, dict):
            subject_name = (
                subject_name.get("zh_cn")
                or subject_name.get("i18n")
                or subject_name.get("en_us")
                or ""
            )
        job_type = " ".join(
            value
            for value in [
                str(recruit_type.get("name") or "").strip()
                if isinstance(recruit_type, dict)
                else "",
                str(subject_name or "").strip(),
            ]
            if value
        ) or "校招"

        description = item.get("description")
        requirement = item.get("requirement")
        fields_are_present = isinstance(description, str) and isinstance(requirement, str)
        jd_raw = (
            normalize_official_jd(description, requirement)
            if fields_are_present
            else ""
        )
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        detail_url = (
            f"{self.HOST}/campus/position/{job_id}/detail"
            if job_id
            else ""
        )
        published_at = ""
        publish_time = item.get("publish_time")
        if isinstance(publish_time, (int, float)) and publish_time:
            published_at = datetime.fromtimestamp(publish_time / 1000).date().isoformat()

        job = self._make_job(
            title=title.strip(),
            city=" / ".join(dict.fromkeys(filter(None, cities)))[:120],
            job_type=job_type,
            jd_url=detail_url,
            jd_raw=jd_raw,
            published_at=published_at,
            link_kind="detail",
        )
        job["detail_url"] = job["jd_url"]
        if job_id:
            # Keep the platform ID alongside the source row; the list-stage
            # identity must survive normalization into storage and hydration.
            job["source_job_id"] = job_id
            job["native_job_id"] = job_id
        job["capture_evidence"] = build_official_api_capture_evidence(
            context=response_context if fields_are_present else None,
            item=item,
            detail_text=jd_raw,
            detail_url=detail_url,
            native_post_id=job_id,
            title=title.strip(),
        )
        return job
