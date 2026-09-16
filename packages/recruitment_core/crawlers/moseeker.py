"""Reusable crawler for public MoSeeker position lists and detail pages."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_DETAIL_PATH_RE = re.compile(r"^/position/index/pid/(\d+)/?$", re.I)
_TOTAL_RE = re.compile(r"\btotalNum\s*:\s*(\d+)\b")
_COHORT_RE = re.compile(r"(?<!\d)(?:20\d{2}|[12]\d)\s*届")
_CAMPAIGN_RE = re.compile(
    r"(?<!\d)(?:20\d{2}|[12]\d)\s*届\s*"
    r"(?:春招|秋招|校招|校园招聘|校园招募|应届生招聘|毕业生招聘|"
    r"暑期实习|日常实习|实习生招聘)",
    re.I,
)


def _clean_text(value: object, *, preserve_lines: bool = True) -> str:
    """Normalize visible HTML text without merging separate JD paragraphs."""
    text = str(value or "").replace("\xa0", " ").replace("\r", "")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) if preserve_lines else " ".join(lines)


def _unique_texts(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        value = _clean_text(value, preserve_lines=False)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


class MoSeekerCrawler(BaseCrawler):
    """Crawl a MoSeeker company list, then hydrate every concrete detail URL.

    MoSeeker renders the current page's rows into ``#position-data`` and puts
    the authoritative list count in ``window.__globalConfig__.totalNum``. The
    crawler intentionally follows the server-rendered ``pageNum`` URL rather
    than depending on the Vue pagination widget.
    """

    PAGE_SIZE = 18
    MAX_PAGES = 100
    JD_RAW_LIMIT = 12000

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.expected_total: int | None = None
        self.total_num: int | None = None
        self.pages_fetched = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []

    def _list_url(self, page_num: int) -> str:
        """Replace only ``pageNum`` while preserving campaign query filters."""
        parts = urlsplit(self.careers_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() != "pagenum"
        ]
        query.append(("pageNum", str(page_num)))
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))

    @staticmethod
    def _pid_from_href(href: str) -> str:
        path = urlsplit(href or "").path
        if path and not path.startswith("/"):
            path = "/" + path
        match = _DETAIL_PATH_RE.match(path)
        return match.group(1) if match else ""

    @classmethod
    def _canonical_detail_url(cls, href: str, base_url: str) -> tuple[str, str]:
        absolute = urljoin(base_url, str(href or "").strip())
        parts = urlsplit(absolute)
        pid = cls._pid_from_href(absolute)
        if not pid:
            return "", ""
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", "")), pid

    @staticmethod
    def _total_from_html(page_html: str) -> int | None:
        match = _TOTAL_RE.search(page_html or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _position_data(soup: BeautifulSoup) -> list[dict]:
        node = soup.find(id="position-data")
        if not node:
            return []
        try:
            payload = json.loads(html_lib.unescape(node.get_text()).strip())
        except (TypeError, json.JSONDecodeError):
            return []
        positions = payload.get("positions") if isinstance(payload, dict) else None
        return [item for item in positions if isinstance(item, dict)] if isinstance(positions, list) else []

    def _parse_list_page(
        self, page_html: str, page_url: str | None = None
    ) -> tuple[list[dict[str, str]], int | None]:
        """Return unique-ready list rows and the page's reported total."""
        page_url = page_url or self.careers_url
        soup = BeautifulSoup(page_html or "", "html.parser")
        rows: list[dict[str, str]] = []
        position_data = self._position_data(soup)

        for item in position_data:
            href = str(item.get("href") or item.get("url") or "").strip()
            pid = self._pid_from_href(href)
            if not pid:
                pid = str(item.get("pid") or item.get("positionId") or "").strip()
                if pid.isdigit():
                    href = f"/position/index/pid/{pid}"
            detail_url, canonical_pid = self._canonical_detail_url(href, page_url)
            pid = canonical_pid or pid
            title = _clean_text(item.get("name") or item.get("title"), preserve_lines=False)
            if not pid or not title or not detail_url:
                continue
            rows.append({
                "pid": pid,
                "title": title,
                "city": _clean_text(
                    item.get("shortCities") or item.get("city") or "",
                    preserve_lines=False,
                ),
                "detail_url": detail_url,
            })

        if not rows:
            rows = self._parse_anchor_rows(soup, page_url)

        unique_rows = []
        seen = set()
        for row in rows:
            if row["pid"] in seen:
                continue
            seen.add(row["pid"])
            unique_rows.append(row)
        return unique_rows, self._total_from_html(page_html)

    def _parse_anchor_rows(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[dict[str, str]]:
        """Fallback for older MoSeeker pages that omit ``#position-data``."""
        rows = []
        for anchor in soup.find_all("a", href=True):
            detail_url, pid = self._canonical_detail_url(anchor.get("href", ""), page_url)
            if not pid or not detail_url:
                continue
            title_node = anchor.select_one(
                ".job-title, .PositionList__job-item__header, h1, h2, h3, h4"
            )
            title = _clean_text(
                title_node.get_text(" ", strip=True) if title_node else "",
                preserve_lines=False,
            )
            if not title:
                title = _clean_text(anchor.get("title") or "", preserve_lines=False)
            if not title:
                continue
            city_node = anchor.select_one(".job-desc-item, .position-text")
            city = _clean_text(
                city_node.get_text(" ", strip=True) if city_node else "",
                preserve_lines=False,
            )
            rows.append({"pid": pid, "title": title, "city": city, "detail_url": detail_url})
        return rows

    @staticmethod
    def _panel_body(soup: BeautifulSoup, label: str):
        for heading in soup.find_all(["h2", "h3", "h4"]):
            if _clean_text(heading.get_text(" ", strip=True), preserve_lines=False) != label:
                continue
            article = heading.find_parent("article")
            if article is None:
                article = heading.parent
            if article is None:
                continue
            body = article.select_one(".qx-postion-panel-body")
            return body or article
        return None

    @classmethod
    def _parse_detail_page(cls, page_html: str) -> dict[str, object]:
        soup = BeautifulSoup(page_html or "", "html.parser")
        description_body = cls._panel_body(soup, "职位描述")
        requirement_body = cls._panel_body(soup, "任职条件")
        attribute_body = cls._panel_body(soup, "职位属性")

        description = _clean_text(
            description_body.get_text("\n", strip=True) if description_body else ""
        )
        requirements = _clean_text(
            requirement_body.get_text("\n", strip=True) if requirement_body else ""
        )

        attribute_lines: list[str] = []
        if attribute_body:
            nodes = attribute_body.select(".postion-attributes-item")
            if nodes:
                attribute_lines = _unique_texts(
                    [node.get_text(" ", strip=True) for node in nodes]
                )
            else:
                attribute_lines = [
                    line for line in _clean_text(
                        attribute_body.get_text("\n", strip=True)
                    ).splitlines()
                    if line
                ]

        attributes: dict[str, str] = {}
        for line in attribute_lines:
            key, separator, value = re.split(r"([：:])", line, maxsplit=1) if re.search(r"[：:]", line) else (line, "", "")
            if separator and key.strip() and value.strip():
                attributes[key.strip()] = value.strip()

        title_node = soup.select_one("h1.postion-title, h2.postion-title")
        title = _clean_text(
            title_node.get_text(" ", strip=True) if title_node else "",
            preserve_lines=False,
        )
        city = ""
        city_node = soup.select_one(".position-text")
        if city_node:
            parts = [part.strip() for part in city_node.get_text(" ", strip=True).split("/") if part.strip()]
            if len(parts) > 1:
                city = parts[-1]

        return {
            "title": title,
            "city": city,
            "description": description,
            "requirements": requirements,
            "attribute_lines": attribute_lines,
            "attributes": attributes,
            "work_nature": attributes.get("工作性质", ""),
            "recruitment_type": attributes.get("招聘类型", ""),
        }

    @staticmethod
    def _campaign_info(title: str) -> tuple[str, str]:
        campaign = _unique_texts(_CAMPAIGN_RE.findall(title))
        cohorts = _unique_texts(_COHORT_RE.findall(title))
        return " ".join(campaign), (cohorts[0] if cohorts else "")

    @classmethod
    def _jd_raw(
        cls,
        detail: dict[str, object],
        cohort_label: str,
    ) -> str:
        parts = []
        description = str(detail.get("description") or "").strip()
        requirements = str(detail.get("requirements") or "").strip()
        attribute_lines = list(detail.get("attribute_lines") or [])
        if description:
            parts.extend(["职位描述", description])
        if requirements:
            parts.extend(["任职条件", requirements])
        if attribute_lines or cohort_label:
            parts.append("职位属性")
            parts.extend(attribute_lines)
            if cohort_label and not any(cohort_label in line for line in attribute_lines):
                parts.append(f"届别：{cohort_label}")
        return "\n".join(str(part).strip() for part in parts if str(part).strip())[: cls.JD_RAW_LIMIT]

    @staticmethod
    def _job_type(detail: dict[str, object], title: str, cohort_label: str) -> str:
        recruitment_type = str(detail.get("recruitment_type") or "校招").strip()
        work_nature = str(detail.get("work_nature") or "").strip()
        return " ".join(_unique_texts([recruitment_type, work_nature, cohort_label])) or "校招"

    def _job_from_row(
        self, row: dict[str, str], detail: dict[str, object]
    ) -> tuple[dict, bool, str]:
        title = str(detail.get("title") or row["title"]).strip()
        city = str(row.get("city") or detail.get("city") or "").strip()
        campaign_text, cohort_label = self._campaign_info(title)
        jd_raw = self._jd_raw(detail, cohort_label)
        description = bool(str(detail.get("description") or "").strip())
        requirements = bool(str(detail.get("requirements") or "").strip())
        work_nature = bool(str(detail.get("work_nature") or "").strip())
        complete = description and requirements and work_nature
        missing = []
        if not description:
            missing.append("职位描述")
        if not requirements:
            missing.append("任职条件")
        if not work_nature:
            missing.append("工作性质")
        job = self._make_job(
            title=title,
            city=city,
            job_type=self._job_type(detail, title, cohort_label),
            jd_url=row["detail_url"],
            jd_raw=jd_raw,
            link_kind="detail",
            campaign_text=campaign_text,
        )
        return job, complete, ",".join(missing)

    def fetch(self) -> list[dict]:
        self.expected_total = None
        self.total_num = None
        self.pages_fetched = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures = []

        rows: list[dict[str, str]] = []
        seen_pids = set()

        for page_num in range(1, self.MAX_PAGES + 1):
            page_url = self._list_url(page_num)
            response = self._get(
                page_url,
                timeout=30,
            )
            if response is None:
                self.pagination_termination_reason = f"list_request_failed_page_{page_num}"
                break

            self.pages_fetched = page_num
            page_rows, page_total = self._parse_list_page(response.text, page_url)
            if self.expected_total is None and page_total is not None:
                self.expected_total = page_total
                self.total_num = page_total
            elif page_total is not None and self.expected_total != page_total:
                logger.warning(
                    "[%s] MoSeeker 第 %d 页 totalNum 从 %s 变为 %s",
                    self.company_name,
                    page_num,
                    self.expected_total,
                    page_total,
                )

            if not page_rows:
                if self.expected_total == len(rows):
                    self.pagination_complete = True
                    self.pagination_termination_reason = "empty_total" if not rows else "total_reached"
                else:
                    self.pagination_termination_reason = f"empty_page_before_total_{page_num}"
                break

            new_count = 0
            for row in page_rows:
                if row["pid"] in seen_pids:
                    continue
                seen_pids.add(row["pid"])
                rows.append(row)
                new_count += 1

            if self.expected_total is not None:
                if len(rows) == self.expected_total:
                    self.pagination_complete = True
                    self.pagination_termination_reason = "total_reached"
                    break
                if len(rows) > self.expected_total:
                    self.pagination_termination_reason = "total_mismatch"
                    break
                if new_count == 0:
                    self.pagination_termination_reason = f"duplicate_page_before_total_{page_num}"
                    break
            elif len(page_rows) < self.PAGE_SIZE:
                self.pagination_complete = True
                self.pagination_termination_reason = "short_page"
                break
            elif new_count == 0:
                self.pagination_termination_reason = f"duplicate_page_{page_num}"
                break
        else:
            self.pagination_termination_reason = "max_pages"

        self.listed_count = len(rows)
        self.detail_expected_total = len(rows)
        jobs: list[dict] = []
        for row in rows:
            response = self._get(
                row["detail_url"],
                timeout=30,
            )
            detail = self._parse_detail_page(response.text) if response is not None else {}
            job, complete, missing = self._job_from_row(row, detail)
            jobs.append(job)
            if complete:
                self.detail_count += 1
            else:
                self.detail_failures.append({
                    "pid": row["pid"],
                    "url": row["detail_url"],
                    "reason": "request_failed" if response is None else f"missing:{missing}",
                })

        self.detail_complete = self.detail_count == self.detail_expected_total
        logger.info(
            "[%s] MoSeeker 抓到 %d/%s 个列表岗位，详情 JD %d/%d，分页=%s(%s)",
            self.company_name,
            self.listed_count,
            self.expected_total if self.expected_total is not None else "?",
            self.detail_count,
            self.detail_expected_total,
            self.pagination_complete,
            self.pagination_termination_reason,
        )
        return jobs


# Keep both spellings available for callers that follow the platform name.
MoseekerCrawler = MoSeekerCrawler
MoseekerRecruitCrawler = MoSeekerCrawler
