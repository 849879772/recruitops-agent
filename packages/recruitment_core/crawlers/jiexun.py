"""捷迅光电官方 2027 届校园招聘爬虫。

官方岗位列表是一个 PHP 页面，当前有两页共 18 个岗位；岗位详情使用
``job.php?id=...``，详情页自身带有 27 届证据和招聘要求。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


_COHORT_MARKER_RE = re.compile(r"(?<!\d)(20\d{2}|2[0-9])\s*届")
_COHORT_2027_RE = re.compile(r"(?<!\d)(?:2027|27)\s*届")
_INTERNSHIP_RE = re.compile(r"实习生?|intern(?:ship)?", re.IGNORECASE)
_EARLY_BATCH_RE = re.compile(r"提前批|提前招聘|提前选拔", re.IGNORECASE)
_PHD_ONLY_RE = re.compile(
    r"仅限博士|只招博士|博士限定|博士及以上|博士学历", re.IGNORECASE
)
_REQUIREMENT_RE = re.compile(r"招聘要求|岗位要求|任职要求|任职资格")
_DATE_RE = re.compile(r"发布时间[：:]\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})")
_SPACE_RE = re.compile(r"[ \t\xa0]+")


def _clean_text(value: object) -> str:
    """Keep the complete visible detail text while removing layout noise."""
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines: list[str] = []
    for line in soup.get_text("\n").splitlines():
        line = _SPACE_RE.sub(" ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


class JiexunCrawler(BaseCrawler):
    """Fetch only official, confirmed 2027 full-time campus positions."""

    OFFICIAL_HOSTS = {"www.hfjiexun.com", "hfjiexun.com"}
    LIST_PATH = "/info.php"
    LIST_CLASS_ID = "106103"
    DETAIL_PATH = "/job.php"
    EXPECTED_PAGES = 2
    EXPECTED_TOTAL = 18
    MAX_DETAIL_TEXT = 12000

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.pages_fetched = 0
        self.page_sizes: list[int] = []
        self.expected_total = self.EXPECTED_TOTAL
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_unique_urls = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []
        self.filtered_internship_count = 0
        self.excluded_records: list[dict[str, str]] = []
        self.metrics: dict[str, Any] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        self.metrics = {
            "pagination_complete": self.pagination_complete,
            "expected_total": self.expected_total,
            "pages_fetched": self.pages_fetched,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "page_sizes": list(self.page_sizes),
            "pagination_termination_reason": self.pagination_termination_reason,
            "detail_complete": self.detail_complete,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_unique_urls": self.detail_unique_urls,
            "detail_failures": list(self.detail_failures),
            "filtered_internship_count": self.filtered_internship_count,
            "excluded_count": len(self.excluded_records),
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return {
            key: self.metrics[key]
            for key in (
                "pagination_complete",
                "expected_total",
                "pages_fetched",
                "raw_listed_count",
                "unique_listed_count",
                "page_sizes",
                "pagination_termination_reason",
            )
        }

    def detail_metrics(self) -> dict[str, Any]:
        return {
            key: self.metrics[key]
            for key in (
                "detail_complete",
                "detail_expected_total",
                "detail_count",
                "detail_unique_urls",
                "detail_failures",
            )
        }

    def _source_is_official(self) -> bool:
        parsed = urlsplit(self.careers_url)
        if parsed.netloc.casefold() not in self.OFFICIAL_HOSTS:
            return False
        if parsed.path.casefold() != self.LIST_PATH:
            return False
        return parse_qs(parsed.query).get("class_id") == [self.LIST_CLASS_ID]

    def _list_url(self, page: int) -> str:
        parts = urlsplit(self.careers_url)
        query = dict(parse_qs(parts.query, keep_blank_values=True))
        query["class_id"] = [self.LIST_CLASS_ID]
        query["page"] = [str(page)]
        flat_query = [(key, value) for key, values in query.items() for value in values]
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(flat_query),
                "",
            )
        )

    def _detail_url(self, source_job_id: str) -> str:
        parts = urlsplit(self.careers_url)
        return urlunsplit((parts.scheme, parts.netloc, self.DETAIL_PATH, f"id={source_job_id}", ""))

    @staticmethod
    def _response_text(response: object) -> str:
        return str(getattr(response, "text", "") or "")

    @staticmethod
    def _parse_source_id(href: str) -> str:
        parsed = urlsplit(href)
        if parsed.path.casefold() != JiexunCrawler.DETAIL_PATH:
            return ""
        source_id = parse_qs(parsed.query).get("id", [""])[0]
        return source_id if source_id.isdigit() else ""

    @classmethod
    def _parse_list_page(cls, html: str, page: int) -> list[dict[str, str]]:
        soup = BeautifulSoup(html or "", "html.parser")
        rows: list[dict[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            source_id = cls._parse_source_id(urljoin("https://www.hfjiexun.com/", anchor["href"]))
            if not source_id:
                continue
            title_node = anchor.select_one(".aitem .title")
            title = " ".join((title_node or anchor).get_text(" ", strip=True).split())
            if title_node is None:
                title = title.split(" 生产制造类", 1)[0].strip()
            city_node = anchor.select_one(".bitem")
            date_node = anchor.select_one(".citem .fbsj")
            published_at = ""
            if date_node:
                match = _DATE_RE.search(date_node.get_text(" ", strip=True))
                published_at = match.group(1) if match else date_node.get_text(" ", strip=True)
            rows.append(
                {
                    "source_job_id": source_id,
                    "title": title,
                    "city": city_node.get_text(" ", strip=True) if city_node else "",
                    "published_at": published_at,
                    "list_page": str(page),
                }
            )
        return rows

    @staticmethod
    def _detail_text(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        detail = soup.select_one(".ajob02")
        return _clean_text(detail or soup)

    @staticmethod
    def _cohort_evidence(text: str) -> str:
        match = _COHORT_2027_RE.search(text or "")
        return match.group(0) if match else ""

    @classmethod
    def _has_conflicting_cohort(cls, text: str) -> bool:
        return any(match.group(1) not in {"2027", "27"} for match in _COHORT_MARKER_RE.finditer(text or ""))

    @classmethod
    def _is_internship(cls, title: str, text: str) -> bool:
        head = "\n".join((text or "").splitlines()[:18])
        return bool(_INTERNSHIP_RE.search(f"{title}\n{head}"))

    @classmethod
    def _is_early_batch(cls, title: str, text: str) -> bool:
        head = "\n".join((text or "").splitlines()[:18])
        return bool(_EARLY_BATCH_RE.search(f"{title}\n{head}"))

    @classmethod
    def _is_phd_only(cls, text: str) -> bool:
        return bool(_PHD_ONLY_RE.search(text or ""))

    @classmethod
    def _has_complete_requirements(cls, text: str) -> bool:
        match = _REQUIREMENT_RE.search(text or "")
        if not match:
            return False
        requirement_body = (text or "")[match.end():].strip()
        return len(requirement_body) >= 20

    def _make_job(self, row: dict[str, str], jd_raw: str, evidence: str) -> dict[str, Any]:
        job = super()._make_job(
            title=row["title"],
            city=row["city"],
            job_type="校招",
            jd_url=self._detail_url(row["source_job_id"]),
            jd_raw=jd_raw[: self.MAX_DETAIL_TEXT],
            published_at=row["published_at"],
            link_kind="detail",
            campaign_text=f"捷迅光电官方详情：{evidence}",
        )
        job.update(
            {
                "source_job_id": row["source_job_id"],
                "cohort": 2027,
                "cohort_status": "confirmed",
                "cohort_source": "捷迅光电官方岗位详情",
                "cohort_evidence": evidence,
                "recruitment_track": "formal",
            }
        )
        # Do not set jd_status here. db.upsert_job derives it from jd_raw.
        return job

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._source_is_official():
            self.pagination_termination_reason = "invalid_official_url"
            self._update_metrics()
            return []

        listed: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for page in range(1, self.EXPECTED_PAGES + 1):
            response = self._get(self._list_url(page), timeout=30)
            if response is None:
                self.pagination_termination_reason = f"list_request_failed_page_{page}"
                self._update_metrics()
                return []
            page_rows = self._parse_list_page(self._response_text(response), page)
            self.pages_fetched = page
            self.page_sizes.append(len(page_rows))
            self.raw_listed_count += len(page_rows)
            for row in page_rows:
                source_id = row["source_job_id"]
                if source_id in seen_ids:
                    self.pagination_termination_reason = f"duplicate_source_id_{source_id}"
                    self._update_metrics()
                    return []
                seen_ids.add(source_id)
                listed.append(row)

        self.unique_listed_count = len(seen_ids)
        self.pagination_complete = (
            self.pages_fetched == self.EXPECTED_PAGES
            and self.unique_listed_count == self.expected_total
        )
        self.pagination_termination_reason = (
            "expected_total_reached"
            if self.pagination_complete
            else "row_count_mismatch"
        )
        self._update_metrics()
        if not self.pagination_complete:
            return []

        accepted: list[tuple[dict[str, str], str, str]] = []
        for row in listed:
            detail_url = self._detail_url(row["source_job_id"])
            response = self._get(detail_url, timeout=30)
            if response is None:
                self.detail_failures.append(
                    {"id": row["source_job_id"], "url": detail_url, "reason": "detail_request_failed"}
                )
                continue
            jd_raw = self._detail_text(self._response_text(response))
            evidence = self._cohort_evidence(jd_raw)
            if not evidence or self._has_conflicting_cohort(jd_raw):
                self.excluded_records.append(
                    {"id": row["source_job_id"], "title": row["title"], "reason": "cohort_evidence_missing_or_conflicting"}
                )
                continue
            if self._is_internship(row["title"], jd_raw):
                self.filtered_internship_count += 1
                self.excluded_records.append(
                    {"id": row["source_job_id"], "title": row["title"], "reason": "internship"}
                )
                continue
            if self._is_early_batch(row["title"], jd_raw):
                self.excluded_records.append(
                    {"id": row["source_job_id"], "title": row["title"], "reason": "early_batch"}
                )
                continue
            if self._is_phd_only(jd_raw):
                self.excluded_records.append(
                    {"id": row["source_job_id"], "title": row["title"], "reason": "phd_only"}
                )
                continue
            if not self._has_complete_requirements(jd_raw):
                self.detail_failures.append(
                    {"id": row["source_job_id"], "url": detail_url, "reason": "detail_jd_incomplete"}
                )
                continue
            accepted.append((row, jd_raw, evidence))

        self.detail_expected_total = len(accepted)
        self.detail_count = len(accepted)
        self.detail_unique_urls = len({self._detail_url(row["source_job_id"]) for row, _, _ in accepted})
        self.detail_complete = (
            not self.detail_failures
            and self.detail_count == self.detail_expected_total
            and self.detail_unique_urls == self.detail_expected_total
        )
        self._update_metrics()
        if not self.detail_complete:
            return []
        return [self._make_job(row, jd_raw, evidence) for row, jd_raw, evidence in accepted]


__all__ = ["JiexunCrawler"]
