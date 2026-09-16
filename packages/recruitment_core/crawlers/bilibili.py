"""bilibili 校招爬虫 —— 自建站 jobs.bilibili.com。

其职位列表 API(/api/campus/position/positionList)带客户端反爬 token(ajSessionId)，
裸 requests 被挡(-101)。但用 Playwright 渲染真实页面时，页面自身 JS 会带上 token，
DOM 正常填充，故走「渲染 + 解析 DOM + 点击翻页」绕过反爬。
列表 DOM：
    <h4 class="item-title"><span class="text">职位标题</span></h4>
页面卡片没有 href，但列表 API 返回稳定职位 ID 和完整 JD，详情路由为
`/campus/positions/<id>`。爬虫优先消费页面自身携带令牌请求到的 API 响应，
DOM 解析仅作为接口结构变化时的降级方案。
"""
import logging
import math
import re
from collections.abc import Mapping

from bs4 import BeautifulSoup

from .base import BaseCrawler, launch_browser

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CITY_RE = re.compile(r"[一-龥]{2,}(?:市|省)")


class BilibiliCrawler(BaseCrawler):
    LIST_URL = "https://jobs.bilibili.com/campus/positions"
    NEXT_PAGE_SELECTOR = (
        "li.ant-pagination-next, button.ant-pagination-next, "
        "button.btn-next, [title='下一页']"
    )
    MAX_PAGES = 30
    JD_RAW_LIMIT = 200

    @staticmethod
    def _int_value(*values):
        for value in values:
            try:
                if isinstance(value, bool) or value in (None, ""):
                    continue
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _request_page_number(body: Mapping) -> int | None:
        return BilibiliCrawler._int_value(
            body.get("pageNum"), body.get("pageNo"), body.get("currentPage")
        )

    @staticmethod
    def _request_scope(body: Mapping) -> dict:
        return {
            key: value
            for key, value in body.items()
            if key not in {"pageNum", "pageNo", "currentPage"}
        }

    def _is_valid_api_page(self, page_number: int | None) -> bool:
        if page_number is None or page_number < 1:
            return False
        upper_bound = self.total_pages if self.total_pages and self.total_pages > 0 else self.MAX_PAGES
        return page_number <= min(self.MAX_PAGES, upper_bound)

    def _reset_pagination_evidence(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False

    def _observe_api_pagination(self, payload: dict, observed_count: int) -> None:
        data = payload.get("data") or {}
        page_info = data.get("page") or data.get("pagination") or {}
        if not isinstance(page_info, Mapping):
            page_info = {}
        total = self._int_value(
            data.get("total"), data.get("totalCount"), data.get("count"),
            page_info.get("total"), page_info.get("totalCount"),
        )
        current = self._int_value(
            data.get("pageNum"), data.get("pageNo"), data.get("currentPage"),
            page_info.get("pageNum"), page_info.get("pageNo"), page_info.get("currentPage"),
        )
        page_size = self._int_value(
            data.get("pageSize"), data.get("size"),
            page_info.get("pageSize"), page_info.get("size"),
        )
        pages = self._int_value(
            data.get("totalPages"), data.get("pages"),
            page_info.get("totalPages"), page_info.get("pages"),
        )
        if total is not None and total >= 0:
            if self.advertised_total is None:
                self.advertised_total = total
            elif self.advertised_total != total:
                self.pagination_complete = False
                self.has_more = True
                self.pagination_termination_reason = "advertised_total_changed"
                return
        if pages is None and total is not None and total >= 0 and page_size and page_size > 0:
            pages = max(1, math.ceil(total / page_size))
        if pages is not None and pages > 0 and self.total_pages is None:
            self.total_pages = pages
        if current is not None and current > 0:
            self.pages_seen = max(self.pages_seen, current)
        if total is not None and total >= 0 and observed_count == total:
            self.pagination_complete = True
            self.has_more = False
            self.pagination_termination_reason = "api_total_reached"

    def fetch(self) -> list[dict]:
        self._reset_pagination_evidence()
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.error("[%s] 未安装 playwright", self.company_name)
            return []

        jobs, seen = [], set()
        api_jobs, api_seen = [], set()
        api_scope: dict | None = None
        api_pages_seen: set[int] = set()
        expected_response_page: int | None = None
        try:
            with sync_playwright() as p:
                browser = launch_browser(
                    p,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                          "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context(user_agent=_UA, viewport={"width": 1366, "height": 768},
                                          locale="zh-CN")
                page = ctx.new_page()

                def request_body(response):
                    try:
                        body = response.request.post_data_json
                    except Exception:  # noqa: BLE001
                        return {}
                    return body if isinstance(body, dict) else {}

                def capture_position_api(response):
                    nonlocal api_scope
                    if "/api/campus/position/positionList" not in response.url:
                        return
                    try:
                        body = request_body(response)
                        if not body:
                            return
                        page_number = self._request_page_number(body)
                        if not self._is_valid_api_page(page_number):
                            return
                        candidate_scope = self._request_scope(body)
                        first_page = api_scope is None
                        if first_page:
                            if page_number != 1:
                                return
                            api_scope = candidate_scope
                        elif candidate_scope != api_scope:
                            return
                        if (
                            not first_page
                            and page_number not in api_pages_seen
                            and page_number != expected_response_page
                        ):
                            return
                        self._parse_api_payload(response.json(), api_jobs, api_seen)
                        api_pages_seen.add(page_number)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[%s] bilibili API 响应解析失败: %s", self.company_name, exc)

                page.on("response", capture_position_api)
                try:
                    page.goto(self.LIST_URL, wait_until="networkidle", timeout=45000)
                except PWTimeout:
                    logger.warning("[%s] goto 超时，仍尝试解析", self.company_name)
                try:
                    page.wait_for_selector(".item-title", timeout=30000)
                except PWTimeout:
                    logger.info("[%s] 未出现职位列表（淡季空？）", self.company_name)

                previous_api_count = 0
                for page_number in range(1, self.MAX_PAGES + 1):
                    page.wait_for_timeout(1000)
                    self.pages_seen = max(self.pages_seen, page_number)
                    new = self._parse(page.content(), jobs, seen)
                    api_new = len(api_seen) - previous_api_count
                    previous_api_count = len(api_seen)
                    if self.pagination_complete:
                        break
                    # 翻页：找「下一页」按钮，禁用/缺失则停
                    nxt = page.locator(self.NEXT_PAGE_SELECTOR)
                    try:
                        visible_next = None
                        for index in range(min(nxt.count(), 20)):
                            candidate = nxt.nth(index)
                            if candidate.is_visible():
                                visible_next = candidate
                                break
                        if visible_next is None:
                            if api_jobs or jobs:
                                self.pagination_complete = bool(
                                    self.advertised_total is not None
                                    and len(api_seen) == self.advertised_total
                                )
                                self.has_more = not self.pagination_complete
                                self.pagination_termination_reason = (
                                    "api_total_reached"
                                    if self.pagination_complete
                                    else "next_control_absent_before_total"
                                )
                            break
                        cls = (visible_next.get_attribute("class") or "") + str(
                            visible_next.get_attribute("disabled")
                        )
                        if "disabled" in cls or visible_next.get_attribute("aria-disabled") == "true":
                            if api_jobs or jobs:
                                self.pagination_complete = bool(
                                    self.advertised_total is not None
                                    and len(api_seen) == self.advertised_total
                                )
                                self.has_more = not self.pagination_complete
                                self.pagination_termination_reason = (
                                    "api_total_reached"
                                    if self.pagination_complete
                                    else "next_control_disabled_before_total"
                                )
                            break
                        if new == 0 and api_new == 0:
                            self.has_more = True
                            self.pagination_termination_reason = "page_did_not_change"
                            break
                        expected_page = max(api_pages_seen or {0}) + 1

                        if api_scope is None:
                            visible_next.click(timeout=5000)
                            page.wait_for_timeout(300)
                            continue

                        if not self._is_valid_api_page(expected_page):
                            self.has_more = True
                            self.pagination_termination_reason = "page_range_exhausted_before_total"
                            break

                        def is_expected_page(response):
                            if "/api/campus/position/positionList" not in response.url:
                                return False
                            body = request_body(response)
                            return (
                                bool(body)
                                and self._request_scope(body) == api_scope
                                and self._request_page_number(body) == expected_page
                                and self._is_valid_api_page(expected_page)
                            )

                        expected_response_page = expected_page
                        try:
                            with page.expect_response(
                                is_expected_page,
                                timeout=15000,
                            ):
                                visible_next.click(timeout=5000)
                        finally:
                            expected_response_page = None
                        if expected_page not in api_pages_seen:
                            self.has_more = True
                            self.pagination_termination_reason = "next_page_response_not_accepted"
                            break
                        page.wait_for_timeout(300)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[%s] bilibili 下一页响应校验失败（当前页 %d）: %s",
                            self.company_name,
                            page_number,
                            exc,
                        )
                        self.has_more = True
                        self.pagination_termination_reason = "next_page_response_failed"
                        break
                else:
                    self.has_more = True
                    self.pagination_termination_reason = "max_pages_reached"

                if self.advertised_total is not None and self.pagination_termination_reason != "advertised_total_changed":
                    self.pagination_complete = len(api_seen) == self.advertised_total
                    self.has_more = not self.pagination_complete
                    if self.pagination_complete:
                        self.pagination_termination_reason = "api_total_reached"
                    elif self.pagination_termination_reason == "api_total_reached":
                        self.pagination_termination_reason = "advertised_total_mismatch"

                ctx.close()
                browser.close()
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] bilibili 爬取异常: %s", self.company_name, e)
            self.fetch_failed = True
            self.pagination_complete = False
            self.pagination_termination_reason = "render_failed"

        result = api_jobs or jobs
        logger.info(
            "[%s] bilibili 抓到 %d 个岗位（%s）",
            self.company_name,
            len(result),
            "官方 API" if api_jobs else "DOM 降级",
        )
        return result

    def _parse_api_payload(self, payload: dict, jobs: list, seen: set) -> int:
        data = payload.get("data") or {}
        rows = data.get("list") or []
        added = 0
        for row in rows:
            position_id = str(row.get("id") or "").strip()
            title = str(row.get("positionName") or row.get("name") or "").strip()
            if not position_id or not title or position_id in seen:
                continue
            seen.add(position_id)
            city = str(row.get("workCity") or row.get("workLocation") or "").strip()
            description = str(
                row.get("positionDescription")
                or row.get("positionDescriptions")
                or ""
            ).strip()
            published_at = str(row.get("pushTime") or row.get("ctime") or "").split(" ", 1)[0]
            jobs.append(self._make_job(
                title=title,
                city=city,
                job_type="校招 正式",
                jd_url=f"{self.LIST_URL}/{position_id}",
                jd_raw=description,
                published_at=published_at,
                link_kind="detail",
            ))
            added += 1
        self._observe_api_pagination(payload, len(seen))
        return added

    def _parse(self, html: str, jobs: list, seen: set) -> int:
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        for h in soup.select(".item-title"):
            span = h.select_one(".text") or h
            title = span.get_text(" ", strip=True)
            if not title or len(title) < 2:
                continue
            key = str(abs(hash(title)) % (10 ** 8))
            if key in seen:
                continue
            seen.add(key)
            # 城市：从所在卡片容器文本里抽
            card = h.find_parent(lambda t: t.has_attr("class") and any(
                "item" in c and "title" not in c for c in t["class"]))
            ctext = card.get_text(" ", strip=True) if card else ""
            m = _CITY_RE.findall(ctext)
            city = "、".join(dict.fromkeys(m))[:40]
            jobs.append(self._make_job(title=title, city=city,
                                       jd_url=f"{self.LIST_URL}#{key}", link_kind="list"))
            new += 1
        return new
