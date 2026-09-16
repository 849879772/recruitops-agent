"""艾飞智控官方 2027 校园招聘爬虫。

艾飞的校招页由 Next.js 服务端渲染，岗位卡和岗位详情都是官方 HTML，
没有必要依赖浏览器或猜测接口。列表页的其余区域包含页脚最新职位、产品链接
和社会招聘入口，因此这里只解析固定的校招岗位列表容器。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
_JOB_PATH_RE = re.compile(r"^/join/[a-z0-9][a-z0-9-]*-2027/?$", re.I)
_INTERNSHIP_RE = re.compile(r"实习|intern", re.I)
_EXPLICIT_NON_FORMAL_RE = re.compile(
    r"(?:职位|岗位)(?:类型|性质|类别)?\s*[：:]\s*"
    r"(?:实习|intern|提前批|社招|社会招聘|往届)"
    r"|提前批|社会招聘|社招|往届",
    re.I,
)
_DETAIL_START_RE = re.compile(r"^(?:职位描述|岗位描述|职位职责|岗位职责|工作职责)$")
_DETAIL_REQUIRED_RE = re.compile(r"^(?:岗位要求|任职要求|任职资格|招聘要求)$")
_DETAIL_END_RE = re.compile(r"^(?:关于艾飞|新闻动态|产品中心|版权声明)")
_LOCATION_RE = re.compile(r"工作地点\s*[:：]?\s*([^\n]+)")


def _clean_text(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").replace("\xa0", " ")).strip()


def _clean_lines(value: object) -> list[str]:
    text = str(value or "").replace("\xa0", " ").replace("\r", "")
    lines = []
    for line in text.split("\n"):
        cleaned = _clean_text(line)
        if cleaned:
            lines.append(cleaned)
    return lines


class AiflyCrawler(BaseCrawler):
    """抓取艾飞智控校招页的 8 条岗位并返回 5 条正式岗位。"""

    OFFICIAL_HOSTS = {"aifly.cn", "www.aifly.cn"}
    CAMPUS_PATH = "/join"
    CAMPAIGN_URL = "https://www.aifly.cn/"
    CAMPAIGN_TEXT = "艾飞智控2027届校园招聘正式启动"
    EXPECTED_TOTAL = 8
    EXPECTED_INTERNSHIP_TOTAL = 3
    EXPECTED_FORMAL_TOTAL = 5
    JD_RAW_LIMIT = 12000
    REQUEST_TIMEOUT = 30
    REQUEST_ATTEMPTS = 3
    RETRY_DELAY = 0.3

    # This is deliberately an allow-list. It prevents footer links and product
    # navigation from becoming jobs when the site adds more content sections.
    LIST_CONTAINER_CLASSES = {
        "flex",
        "flex-col",
        "divide-y",
        "divide-border/40",
    }

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.expected_total: int | None = None
        self.listed_count = 0
        self.unique_listed_count = 0
        self.pages_fetched = 0
        self.page_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []
        self.filtered_internship_count = 0
        self.formal_count = 0
        self.campaign_validated = False
        self.campaign_evidence = ""
        self.metrics: dict[str, Any] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        self.metrics = {
            "expected_total": self.expected_total,
            "listed_count": self.listed_count,
            "unique_listed_count": self.unique_listed_count,
            "pages_fetched": self.pages_fetched,
            "page_count": self.page_count,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
            "filtered_internship_count": self.filtered_internship_count,
            "formal_count": self.formal_count,
            "campaign_validated": self.campaign_validated,
            "campaign_evidence": self.campaign_evidence,
        }

    def pagination_metrics(self) -> dict[str, object]:
        return {
            "expected_total": self.expected_total,
            "listed_count": self.listed_count,
            "unique_listed_count": self.unique_listed_count,
            "pages_fetched": self.pages_fetched,
            "page_count": self.page_count,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
        }

    def detail_metrics(self) -> dict[str, object]:
        return {
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
        }

    @classmethod
    def _is_official_campus_url(cls, url: str) -> bool:
        parsed = urlsplit(url or "")
        return (
            parsed.netloc.casefold() in cls.OFFICIAL_HOSTS
            and parsed.path.rstrip("/").casefold() == cls.CAMPUS_PATH
            and not parsed.query
            and not parsed.fragment
        )

    @classmethod
    def _find_list_container(cls, soup: BeautifulSoup):
        candidates = []
        for node in soup.find_all("div"):
            classes = set(node.get("class") or [])
            if not cls.LIST_CONTAINER_CLASSES.issubset(classes):
                continue
            if node.find("a", href=_JOB_PATH_RE):
                candidates.append(node)
        if len(candidates) != 1:
            return None
        return candidates[0]

    @classmethod
    def _result_count(cls, soup: BeautifulSoup) -> int | None:
        text = _clean_text(soup.get_text(" ", strip=True))
        match = re.search(r"职位筛选\s*(\d+)\s*结果", text)
        return int(match.group(1)) if match else None

    @classmethod
    def _parse_list(cls, page_html: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(page_html or "", "html.parser")
        container = cls._find_list_container(soup)
        if container is None:
            return []

        rows = []
        seen_urls: set[str] = set()
        for anchor in container.find_all("a", href=True):
            href = _clean_text(anchor.get("href"))
            absolute_url = urljoin("https://www.aifly.cn/join", href)
            parsed = urlsplit(absolute_url)
            if (
                parsed.netloc.casefold() not in cls.OFFICIAL_HOSTS
                or not _JOB_PATH_RE.fullmatch(parsed.path)
                or parsed.query
                or parsed.fragment
                or absolute_url in seen_urls
            ):
                continue
            title = _clean_text(anchor.get_text(" ", strip=True))
            if not title or _EXPLICIT_NON_FORMAL_RE.search(title):
                return []
            card = anchor.find_parent("div", class_=lambda value: value and "group" in value)
            card_text = _clean_text(card.get_text(" ", strip=True) if card else anchor.parent.get_text(" ", strip=True))
            date_match = _DATE_RE.search(card_text)
            seen_urls.add(absolute_url)
            rows.append({
                "title": title,
                "jd_url": absolute_url,
                "published_at": date_match.group(1) if date_match else "",
            })
        return rows

    @classmethod
    def _has_campaign_evidence(cls, page_html: str) -> bool:
        soup = BeautifulSoup(page_html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = re.sub(r"\s+", "", soup.get_text(" ", strip=True))
        return cls.CAMPAIGN_TEXT in text

    @classmethod
    def _extract_detail(cls, page_html: str, expected_title: str) -> tuple[str, str]:
        soup = BeautifulSoup(page_html or "", "html.parser")
        main = soup.find("main")
        if main is None:
            return "", ""
        for tag in main(["script", "style", "noscript", "svg"]):
            tag.decompose()

        title_node = main.find("h1")
        title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
        if title.replace(" ", "") != expected_title.replace(" ", ""):
            return "", title

        lines = _clean_lines(main.get_text("\n", strip=True))
        start = next(
            (index for index, line in enumerate(lines) if _DETAIL_START_RE.fullmatch(line)),
            None,
        )
        if start is None:
            return "", title

        end = len(lines)
        for index in range(start + 1, len(lines)):
            if _DETAIL_END_RE.search(lines[index]) or lines[index] == "关于艾飞":
                end = index
                break
        detail_lines = lines[start:end]
        detail = "\n".join(detail_lines).strip()
        has_requirements_heading = any(
            _DETAIL_REQUIRED_RE.fullmatch(line) for line in detail_lines
        )
        if not has_requirements_heading or len(detail) < 120:
            return "", title
        return detail[: cls.JD_RAW_LIMIT], title

    @staticmethod
    def _extract_city(jd_raw: str) -> str:
        match = _LOCATION_RE.search(jd_raw or "")
        return _clean_text(match.group(1)) if match else ""

    def _new_session(self):
        return requests.Session()

    def _request(self, session, url: str):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.careers_url,
        }
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = session.get(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] 艾飞请求失败（%d 次）: %s",
                        self.company_name,
                        self.REQUEST_ATTEMPTS,
                        exc,
                    )
                    return None
                time.sleep(self.RETRY_DELAY * attempt)
        return None

    def _make_formal_job(self, row: dict[str, str], jd_raw: str) -> dict:
        job = self._make_job(
            title=row["title"],
            city=self._extract_city(jd_raw),
            job_type="校招",
            jd_url=row["jd_url"],
            jd_raw=jd_raw,
            published_at=row["published_at"],
            link_kind="detail",
            campaign_text=self.CAMPAIGN_TEXT,
        )
        job.update({
            "cohort": 2027,
            "cohort_status": "confirmed",
            "cohort_source": "官网2027届校招正式启动证据",
            "cohort_evidence": self.CAMPAIGN_TEXT,
            "recruitment_track": "formal",
        })
        return job

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._is_official_campus_url(self.careers_url):
            self.pagination_termination_reason = "wrong_official_campus_page"
            self._update_metrics()
            return []

        session = self._new_session()
        list_response = self._request(session, self.careers_url)
        if list_response is None:
            self.pagination_termination_reason = "list_request_failed"
            self._update_metrics()
            return []

        self.pages_fetched = 1
        self.page_count = 1
        self.expected_total = self._result_count(
            BeautifulSoup(list_response.text, "html.parser")
        )
        rows = self._parse_list(list_response.text)
        self.listed_count = len(rows)
        self.unique_listed_count = len({row["jd_url"] for row in rows})
        self.detail_expected_total = self.expected_total or 0

        if self.expected_total != self.EXPECTED_TOTAL:
            self.pagination_termination_reason = "list_total_mismatch"
            self._update_metrics()
            return []
        if (
            self.listed_count != self.EXPECTED_TOTAL
            or self.unique_listed_count != self.EXPECTED_TOTAL
        ):
            self.pagination_termination_reason = "list_container_incomplete"
            self._update_metrics()
            return []

        evidence_response = self._request(session, self.CAMPAIGN_URL)
        if evidence_response is None or not self._has_campaign_evidence(evidence_response.text):
            self.pagination_termination_reason = "cohort_evidence_missing"
            self._update_metrics()
            return []
        self.campaign_validated = True
        self.campaign_evidence = self.CAMPAIGN_TEXT
        self.pagination_complete = True
        self.pagination_termination_reason = "single_static_page"

        hydrated: list[dict] = []
        for row in rows:
            detail_response = self._request(session, row["jd_url"])
            if detail_response is None:
                self.detail_failures.append({
                    "title": row["title"],
                    "url": row["jd_url"],
                    "reason": "detail_request_failed",
                })
                continue
            jd_raw, detail_title = self._extract_detail(detail_response.text, row["title"])
            if (
                not jd_raw
                or not detail_title
                or _EXPLICIT_NON_FORMAL_RE.search(jd_raw)
                or not any(
                    _DETAIL_REQUIRED_RE.fullmatch(line)
                    for line in _clean_lines(jd_raw)
                )
            ):
                self.detail_failures.append({
                    "title": row["title"],
                    "url": row["jd_url"],
                    "reason": "detail_content_incomplete_or_non_campus",
                })
                continue
            self.detail_count += 1
            hydrated.append({"row": row, "jd_raw": jd_raw})

        self.detail_complete = self.detail_count == self.detail_expected_total
        if not self.detail_complete:
            self.pagination_termination_reason = "detail_incomplete"
            self._update_metrics()
            return []

        formal_jobs = []
        for item in hydrated:
            row = item["row"]
            if _INTERNSHIP_RE.search(row["title"]):
                self.filtered_internship_count += 1
                continue
            formal_jobs.append(self._make_formal_job(row, item["jd_raw"]))

        self.formal_count = len(formal_jobs)
        if (
            self.filtered_internship_count != self.EXPECTED_INTERNSHIP_TOTAL
            or self.formal_count != self.EXPECTED_FORMAL_TOTAL
            or len(formal_jobs) + self.filtered_internship_count != self.EXPECTED_TOTAL
        ):
            self.pagination_termination_reason = "formal_internship_split_mismatch"
            self._update_metrics()
            return []

        self._update_metrics()
        return formal_jobs


__all__ = ["AiflyCrawler"]
