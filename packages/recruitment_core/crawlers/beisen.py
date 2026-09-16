"""北森 ATS（*.zhiye.com）通用校招爬虫基类。

师兄清单里 ~106 家用北森，URL 形如 https://<subdomain>.zhiye.com/...
现代北森校招 UI 在 `/campus/jobs`，岗位标题渲染在 DOM 里（styled-components）：
    <div class="...STListItemContent...">
      <div class="...STTitleSection...">
        <div class="...STJobTitle...">【代码】岗位标题</div>
优先调用北森 2022 门户 API 获取岗位列表和真实详情页链接；API 不可用时回退 DOM 渲染。

子类无需覆盖——careers_url 给任意北森页，基类按子域名拼 /campus/jobs 抓取。
注：少数老租户只有旧版 /Portal/Apply/Index（DOM 不同），本基类抓不到会返回空（优雅降级）。
"""
import logging
import math
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler, effective_crawl_timeout_seconds
from .render import render_page

logger = logging.getLogger(__name__)


class BeisenRecruitCrawler(BaseCrawler):
    EXTRA_WAIT_MS = 6000
    SCROLL_TIMES = 8
    JD_RAW_LIMIT = 12000
    PAGE_SIZE = 100
    API_RETRIES = 3
    API_RETRY_BACKOFF_SECONDS = 1.0
    # 北森列表页的栏目标题也带 STJobTitle 类，需剔除，避免写成假岗位
    _SKIP_TITLES = {"热招职位", "热门职位", "推荐职位", "热招岗位", "在招职位", "全部职位"}
    # 华曦达当前招聘子域名证书链已过期。仅对此已知官方域名放宽校验，
    # 避免把 TLS 绕过扩散到其他北森租户。
    INSECURE_TLS_HOSTS = {"job.sdmctech.com"}

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.api_expected_total: int | None = None
        self.pages_seen = 0
        self.total_pages: int | None = None
        self.advertised_total: int | None = None
        self.has_more = False
        self._crawl_deadline: float | None = None
        self.crawl_error_code = ""
        self.crawl_error_message = ""

    def _remaining_seconds(self, fallback: float) -> float:
        if self._crawl_deadline is None:
            return max(0.05, fallback)
        return max(0.05, min(fallback, self._crawl_deadline - time.monotonic()))

    def _budget_exhausted(self) -> bool:
        return self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline

    def _list_url(self) -> str:
        parsed = urlparse(self.careers_url)
        host = parsed.netloc
        # Newer Beisen tenants can expose a dedicated campus programme as
        # ``/<category>/jobs`` (for example iFlytek's 飞凡计划 at ``/5/jobs``).
        # Preserve that route instead of silently falling back to the default
        # campus category, which can be empty while the programme is active.
        match = re.match(r"^/(\d+)/jobs/?$", parsed.path)
        if match:
            return f"https://{host}/{match.group(1)}/jobs"
        # Legacy Beisen portals use /campus query parameters for department
        # filters. Preserve them, otherwise a subsidiary source silently
        # expands to the whole parent organisation.
        if parsed.path.rstrip("/").casefold() == "/campus" and parsed.query:
            return urlunparse((
                parsed.scheme or "https",
                host,
                parsed.path,
                "",
                parsed.query,
                "",
            ))
        return f"https://{host}/campus/jobs"

    def _category(self) -> str:
        match = re.search(r"/(\d+)/jobs/?$", urlparse(self._list_url()).path)
        return match.group(1) if match else "2"

    def _origin(self) -> str:
        p = urlparse(self._list_url())
        return f"{p.scheme}://{p.netloc}"

    def _api_url(self) -> str:
        return f"{self._origin()}/api/Jobad/GetJobAdPageList"

    def _verify_tls(self) -> bool:
        return urlparse(self._list_url()).netloc.casefold() not in self.INSECURE_TLS_HOSTS

    def _detail_url(self, job_ad_id: str) -> str:
        path = urlparse(self._list_url()).path
        prefix = path.rsplit("/jobs", 1)[0]
        if prefix and prefix != "/campus":
            return f"{self._origin()}{prefix}/detail?jobAdId={job_ad_id}"
        return f"{self._origin()}/campus/detail?jobAdId={job_ad_id}"

    def _api_payload(self, page_index: int) -> dict:
        return {
            "PageIndex": page_index,
            "PageSize": self.PAGE_SIZE,
            "Category": [self._category()],
            "KeyWords": "",
            "SpecialType": 0,
            "PortalId": "",
            "DisplayFields": [
                "Category", "Kind", "LocId", "PostDate", "WorkWeChatQrCode",
            ],
        }

    def _request_api_page(self, session: requests.Session, headers: dict, page_index: int) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, self.API_RETRIES + 1):
            if self._budget_exhausted():
                raise TimeoutError("北森 API crawl deadline exhausted")
            try:
                request_kwargs = {
                    "json": self._api_payload(page_index),
                    "headers": headers,
                    "timeout": self._remaining_seconds(25),
                }
                if not self._verify_tls():
                    request_kwargs["verify"] = False
                resp = session.post(self._api_url(), **request_kwargs)
                resp.raise_for_status()
                data = resp.json()
                if data.get("Code") != 200:
                    raise RuntimeError(data.get("Message") or "北森 API 返回非 200")
                return data
            except Exception as exc:
                last_error = exc
                if attempt >= self.API_RETRIES:
                    break
                logger.warning(
                    "[%s] 北森 API 第 %d 页失败，重试 %d/%d：%s",
                    self.company_name,
                    page_index + 1,
                    attempt,
                    self.API_RETRIES - 1,
                    exc,
                )
                if self._crawl_deadline is not None:
                    remaining = self._crawl_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(
                        self.API_RETRY_BACKOFF_SECONDS * attempt,
                        remaining,
                    ))
                else:
                    time.sleep(self.API_RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError(
            f"北森 API 第 {page_index + 1} 页连续失败"
        ) from last_error

    def _fetch_api_jobs(self) -> list[dict]:
        session = requests.Session()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self._origin(),
            "Referer": self._list_url(),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
        }

        jobs: list[dict] = []
        seen = set()
        total = None
        page_index = 0
        while True:
            data = self._request_api_page(session, headers, page_index)
            self.pages_seen = page_index + 1
            reported_total = data.get("Total")
            count_total = data.get("Count")
            # Some Beisen tenants return Total=0 while Count contains the
            # actual total, even when Data is non-empty.
            if (
                reported_total is None
                or int(reported_total) <= 0
            ) and count_total is not None:
                reported_total = count_total
            if reported_total is not None:
                total = int(reported_total)
                self.api_expected_total = total
                self.advertised_total = total
                self.total_pages = math.ceil(total / self.PAGE_SIZE) if total else 0

            rows = data.get("Data") or []
            if not isinstance(rows, list) or not rows:
                if total is not None and len(jobs) < total:
                    raise RuntimeError(
                        f"北森 API 在 {len(jobs)}/{total} 时提前返回空页"
                    )
                self.pagination_complete = True
                self.pagination_termination_reason = "api_empty_page"
                break

            for row in rows:
                title = (row.get("JobAdName") or "").strip()
                job_id = str(row.get("Id") or "").strip()
                if not title or not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                locs = row.get("LocNames") or []
                city = "、".join(str(x) for x in locs if x)[:80]
                duty = str(row.get("Duty") or "").strip()
                requirement = str(row.get("Require") or "").strip()
                jd_parts = []
                if duty:
                    jd_parts.extend(["岗位职责", duty])
                if requirement:
                    jd_parts.extend(["任职要求", requirement])
                jd_raw = "\n".join(jd_parts)[: self.JD_RAW_LIMIT]
                jobs.append(self._make_job(
                    title=title,
                    city=city,
                    job_type=str(row.get("Kind") or "校招").strip(),
                    jd_url=self._detail_url(job_id),
                    jd_raw=jd_raw,
                    published_at=str(row.get("PostDate") or "").strip()[:20],
                ))

            page_index += 1
            self.has_more = total is not None and len(jobs) < total
            if total is not None and len(jobs) >= int(total):
                self.pagination_complete = len(jobs) == total
                self.pagination_termination_reason = (
                    "api_total_reached"
                    if self.pagination_complete
                    else "api_total_mismatch"
                )
                break
            if len(rows) < self.PAGE_SIZE:
                if total is not None and len(jobs) < total:
                    raise RuntimeError(
                        f"北森 API 短页结束但仅获得 {len(jobs)}/{total}"
                    )
                self.pagination_complete = True
                self.pagination_termination_reason = "api_short_page"
                break

        if total is not None and len(jobs) != total:
            raise RuntimeError(f"北森 API 结果不完整：{len(jobs)}/{total}")
        self.has_more = False
        return jobs

    @staticmethod
    def _legacy_page_url(list_url: str, page_index: int) -> str:
        parsed = urlparse(list_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() != "pageindex"
        ]
        if page_index > 1:
            query.append(("PageIndex", str(page_index)))
        path = parsed.path
        if path.rstrip("/").casefold() == "/campus/jobs":
            path = "/campus/"
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            urlencode(query),
            "",
        ))

    def _parse_legacy_page(self, html: str) -> tuple[list[dict], int]:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []
        for anchor in soup.select("a[href*='/zpdetail/']"):
            title = anchor.get_text(" ", strip=True)
            href = str(anchor.get("href") or "").strip()
            if not title or not re.search(r"/zpdetail/\d+", href, re.I):
                continue
            cells = []
            row = anchor.find_parent("tr")
            if row:
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in row.find_all("td", recursive=False)
                ]
            jobs.append(self._make_job(
                title=title,
                city=cells[2][:80] if len(cells) > 2 else "",
                jd_url=urljoin(self._origin(), href),
                published_at=cells[3][:20] if len(cells) > 3 else "",
            ))
        page_indexes = [
            int(match.group(1))
            for anchor in soup.find_all("a", href=True)
            if (match := re.search(
                r"[?&]PageIndex=(\d+)",
                str(anchor.get("href") or ""),
                re.I,
            ))
        ]
        return jobs, max(page_indexes, default=1)

    def _fetch_legacy_jobs(self) -> list[dict]:
        """Fetch every page from pre-2022 Beisen portals."""
        if self._budget_exhausted():
            self.pagination_complete = False
            self.pagination_termination_reason = "hard_timeout"
            return []
        first_url = self._legacy_page_url(self._list_url(), 1)
        first = self._get(
            first_url,
            verify=False,
            timeout=self._remaining_seconds(25),
        )
        if not first or urlparse(first.url).path.rstrip("/").endswith("/404"):
            return []
        first.encoding = first.apparent_encoding or first.encoding
        first_jobs, last_page = self._parse_legacy_page(first.text)
        if not first_jobs:
            return []

        jobs = []
        seen = set()

        def append_unique(rows: list[dict]) -> None:
            for job in rows:
                identity = job["jd_url"]
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(job)

        append_unique(first_jobs)
        for page_index in range(2, last_page + 1):
            if self._budget_exhausted():
                self.pagination_complete = False
                self.pagination_termination_reason = "hard_timeout"
                return jobs
            page_url = self._legacy_page_url(self._list_url(), page_index)
            response = self._get(
                page_url,
                verify=False,
                timeout=self._remaining_seconds(25),
            )
            if not response:
                self.pagination_complete = False
                self.pagination_termination_reason = "legacy_page_failed"
                return jobs
            response.encoding = response.apparent_encoding or response.encoding
            page_jobs, _ = self._parse_legacy_page(response.text)
            if not page_jobs:
                self.pagination_complete = False
                self.pagination_termination_reason = "legacy_empty_page"
                return jobs
            append_unique(page_jobs)

        self.pagination_complete = True
        self.pagination_termination_reason = "legacy_all_pages"
        return jobs

    def fetch(self) -> list[dict]:
        self._crawl_deadline = time.monotonic() + effective_crawl_timeout_seconds(120.0)
        list_url = self._list_url()
        api_failed = False
        try:
            api_jobs = self._fetch_api_jobs()
            if api_jobs:
                logger.info("[%s] 北森 API 抓到 %d 个岗位", self.company_name, len(api_jobs))
                return api_jobs
        except Exception as e:
            api_failed = True
            self.pagination_complete = False
            self.crawl_error_code = "api_failed"
            self.crawl_error_message = str(e)[-500:]
            self.pagination_termination_reason = (
                "hard_timeout" if self._budget_exhausted() else "api_failed"
            )
            logger.warning("[%s] 北森 API 抓取失败，回退渲染：%s", self.company_name, e)

        if api_failed and not self._budget_exhausted():
            legacy_jobs = self._fetch_legacy_jobs()
            if legacy_jobs:
                logger.info(
                    "[%s] 北森旧版门户完整抓到 %d 个岗位",
                    self.company_name,
                    len(legacy_jobs),
                )
                return legacy_jobs

        html = render_page(list_url, wait_for=None, timeout_ms=45000,
                           extra_wait_ms=self.EXTRA_WAIT_MS, scroll_times=self.SCROLL_TIMES)
        if not html:
            logger.warning("[%s] 北森 渲染失败", self.company_name)
            return []

        soup = BeautifulSoup(html, "html.parser")
        title_els = soup.find_all(
            lambda t: t.has_attr("class") and any("STJobTitle" in c for c in t["class"])
        )
        jobs = []
        seen = set()
        for el in title_els:
            title = el.get_text(" ", strip=True)
            if not title or len(title) < 2:
                continue
            if title in self._SKIP_TITLES:
                continue
            # 提取【代码】作唯一标识；无则用标题哈希兜底
            m = re.match(r"^[【\[]([^】\]]+)[】\]]", title)
            key = m.group(1) if m else str(abs(hash(title)) % (10 ** 8))
            if key in seen:
                continue
            seen.add(key)
            jd_url = f"{list_url}#{key}"
            # 城市：在所属列表项容器里找含 City/Location/地点 的标签
            container = el.find_parent(
                lambda t: t.has_attr("class") and any("STListItem" in c for c in t["class"])
            )
            city = ""
            if container:
                city_el = container.find(
                    lambda t: t.has_attr("class")
                    and any(("City" in c or "Location" in c or "Address" in c) for c in t["class"])
                )
                if city_el:
                    city = city_el.get_text(" ", strip=True)[:40]
            jobs.append(self._make_job(title=title, city=city, jd_url=jd_url))

        logger.info("[%s] 北森 抓到 %d 个岗位", self.company_name, len(jobs))
        if api_failed and jobs:
            self.pagination_complete = False
            self.pagination_termination_reason = "api_failed_render_fallback"
            logger.error(
                "[%s] 北森 API 失败后的渲染结果仅用于诊断，不进入数据库",
                self.company_name,
            )
        elif jobs:
            self.pagination_complete = True
            self.pagination_termination_reason = "render_after_empty_api"
        return jobs
