"""SoftStone official 2027 campus-recruitment API crawler.

The official list API returns 15 rows over three pages.  This crawler first
proves that the complete source list was collected, then filters by official
2027/full-time evidence and hydrates the three retained detail records.  A
missing detail is retained as a pending job instead of being fabricated or
dropped.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_COHORT_RE = re.compile(r"(?<!\d)(20\d{2}|2[0-9])\s*届", re.I)
_MIXED_COHORT_RE = re.compile(r"(?:20\d{2}|2[0-9])\s*/\s*(?:20\d{2}|2[0-9])\s*届", re.I)
_INTERNSHIP_RE = re.compile(r"实习|intern(?:ship)?", re.I)
_NON_FORMAL_RE = re.compile(r"提前批|提前招聘|社招|社会招聘|博士限定|博士岗", re.I)
_DUTY_RE = re.compile(r"岗位职责|职位描述|工作职责|岗位描述|工作内容|职位介绍|岗位简介", re.I)
_REQUIREMENT_RE = re.compile(r"任职要求|岗位要求|职位要求|任职资格|学历要求", re.I)


class IsoftstoneCrawler(BaseCrawler):
    """Fetch SoftStone's public campus API without an LLM dependency."""

    OFFICIAL_HOST = "career.isoftstone.com"
    LIST_PATH = "/talent/htmls/xiaoyuanzhaopin/index.html"
    DETAIL_PATH = "/talent/htmls/xiaozhaozhiweixiangqing/index.html"
    LIST_API = "https://career.isoftstone.com/campus/all"
    DETAIL_API = "https://career.isoftstone.com/campus/detail"
    PAGE_SIZE = 6
    MAX_PAGES = 100
    REQUEST_ATTEMPTS = 3
    DETAIL_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 0.2
    JD_RAW_LIMIT = 12000

    def __init__(self, company_name: str, careers_url: str, *, session=None):
        super().__init__(company_name, careers_url)
        self.session = session or requests.Session()
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.pages_fetched = 0
        self.expected_total: int | None = None
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.page_sizes: list[int] = []
        self.pagination_duplicate_ids: list[str] = []
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_expected_total = 0
        self.detail_api_calls = 0
        self.detail_count = 0
        self.detail_unique_urls = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []
        self.filtered_counts = {"internship": 0, "mixed_cohort": 0, "historical_cohort": 0,
                                "non_formal": 0, "missing_2027_evidence": 0}
        self.excluded_records: list[dict[str, str]] = []
        self.retained_count = 0
        self.metrics: dict[str, Any] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        pagination = {
            "pages_fetched": self.pages_fetched,
            "expected_total": self.expected_total,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "page_sizes": list(self.page_sizes),
            "duplicate_ids": list(self.pagination_duplicate_ids),
            "pagination_complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
        }
        detail = {
            "expected_total": self.detail_expected_total,
            "api_calls": self.detail_api_calls,
            "count": self.detail_count,
            "unique_urls": self.detail_unique_urls,
            "complete": self.detail_complete,
            "failures": list(self.detail_failures),
        }
        self.metrics = {
            "pagination_complete": self.pagination_complete,
            "expected_total": self.expected_total,
            "detail_count": self.detail_count,
            "detail_complete": self.detail_complete,
            "retained_count": self.retained_count,
            "filtered_counts": dict(self.filtered_counts),
            "excluded_count": len(self.excluded_records),
            "pagination": pagination,
            "detail": detail,
            "detail_failures": list(self.detail_failures),
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return dict(self.metrics["pagination"])

    def detail_metrics(self) -> dict[str, Any]:
        return dict(self.metrics["detail"])

    @classmethod
    def _is_official_url(cls, url: str) -> bool:
        parsed = urlparse(url or "")
        return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == cls.OFFICIAL_HOST and parsed.path == cls.LIST_PATH

    @classmethod
    def _detail_url(cls, source_id: str) -> str:
        return f"https://{cls.OFFICIAL_HOST}{cls.DETAIL_PATH}?id={source_id}&recruitType=1"

    def _headers(self, *, json_request: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.careers_url,
            "Origin": f"https://{self.OFFICIAL_HOST}",
        }
        if json_request:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    def _warm_session(self) -> None:
        try:
            response = self.session.get(self.careers_url, headers=self._headers(), timeout=30)
            response.raise_for_status()
        except (requests.RequestException, AttributeError) as exc:
            logger.warning("[%s] official page warm-up failed: %s", self.company_name, exc)

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = self.session.post(self.LIST_API if "page" in payload else self.DETAIL_API,
                                             json=payload, headers=self._headers(json_request=True), timeout=30)
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else None
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning("[%s] API request failed: %s", self.company_name, exc)
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
        return None

    @staticmethod
    def _row_text(row: dict[str, Any]) -> str:
        keys = ("name", "title", "hiretype_name", "job_type", "recruitment_type", "labels", "educational_name")
        return "\n".join(str(row.get(key) or "").strip() for key in keys)

    @classmethod
    def _has_2027(cls, text: str) -> bool:
        return bool(re.search(r"(?<!\d)(?:2027|27)\s*届", text or ""))

    @classmethod
    def _cohort_reason(cls, row: dict[str, Any]) -> str | None:
        text = cls._row_text(row)
        if _MIXED_COHORT_RE.search(text):
            return "mixed_cohort"
        markers = [re.sub(r"\s+", "", m.group(0)) for m in _COHORT_RE.finditer(text)]
        if any(not marker.startswith(("2027", "27")) for marker in markers):
            return "historical_cohort"
        if not cls._has_2027(text):
            return "missing_2027_evidence"
        return None

    @classmethod
    def _exclude_reason(cls, row: dict[str, Any]) -> str | None:
        cohort_reason = cls._cohort_reason(row)
        if cohort_reason:
            return cohort_reason
        text = cls._row_text(row)
        if _INTERNSHIP_RE.search(text):
            return "internship"
        if _NON_FORMAL_RE.search(text):
            return "non_formal"
        return None

    @staticmethod
    def _clean_text(value: Any) -> str:
        value = html.unescape(str(value or ""))
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()

    @classmethod
    def _compose_jd(cls, detail: dict[str, Any]) -> str:
        description = cls._clean_text(detail.get("describe") or detail.get("job_describe"))
        requirements = cls._clean_text(detail.get("jobrequirements") or detail.get("job_require"))
        if description and not _DUTY_RE.search(description):
            description = f"职位描述\n{description}"
        if requirements and not _REQUIREMENT_RE.search(requirements):
            requirements = f"任职要求\n{requirements}"
        return "\n\n".join(part for part in (description, requirements) if part)[: cls.JD_RAW_LIMIT]

    @classmethod
    def _complete_jd(cls, text: str) -> bool:
        text = str(text or "").strip()
        return bool(text and len(text) >= 40 and _DUTY_RE.search(text) and _REQUIREMENT_RE.search(text))

    def _list_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        next_page = 1
        for _ in range(self.MAX_PAGES):
            payload = {"workCity": "", "jobTypeId": "0", "keyWord": "", "maxcount": self.PAGE_SIZE, "page": next_page}
            response = self._post_json(payload)
            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                self.pagination_termination_reason = f"invalid_list_response_page_{next_page}"
                self._update_metrics()
                return []
            try:
                total = int(response.get("count"))
            except (TypeError, ValueError):
                self.pagination_termination_reason = "missing_expected_total"
                self._update_metrics()
                return []
            if self.expected_total is None:
                self.expected_total = total
            elif self.expected_total != total:
                self.pagination_termination_reason = "expected_total_changed"
                self._update_metrics()
                return []
            items = response["results"]
            self.pages_fetched = next_page
            self.page_sizes.append(len(items))
            self.raw_listed_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    self.pagination_termination_reason = "invalid_list_row"
                    self._update_metrics()
                    return []
                source_id = str(item.get("id") or "").strip()
                if not source_id:
                    self.pagination_termination_reason = "missing_source_id"
                    self._update_metrics()
                    return []
                if source_id in seen_ids:
                    self.pagination_duplicate_ids.append(source_id)
                    continue
                seen_ids.add(source_id)
                rows.append(item)
            next_value = response.get("next")
            if next_value in (None, ""):
                self.unique_listed_count = len(seen_ids)
                self.pagination_complete = (
                    self.expected_total == self.unique_listed_count
                    and not self.pagination_duplicate_ids
                )
                self.pagination_termination_reason = "final_page" if self.pagination_complete else "row_count_mismatch"
                self._update_metrics()
                return rows if self.pagination_complete else []
            next_page += 1
        self.pagination_termination_reason = "max_pages"
        self.unique_listed_count = len(seen_ids)
        self._update_metrics()
        return []

    def _fetch_detail(self, source_id: str) -> dict[str, Any] | None:
        for attempt in range(1, self.DETAIL_ATTEMPTS + 1):
            self.detail_api_calls += 1
            response = self._post_json({"id": source_id})
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(data, dict):
                return data
            if attempt < self.DETAIL_ATTEMPTS:
                time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
        return None

    def _make_job_record(self, row: dict[str, Any], detail: dict[str, Any] | None, detail_url: str,
                         jd_raw: str, status: str) -> dict[str, Any]:
        source = dict(row)
        if detail:
            source.update(detail)
        source_id = str(row.get("id") or "").strip()
        title = str(source.get("name") or row.get("name") or "").strip()
        job = self._make_job(title=title, city=str(source.get("address_detail") or row.get("address_detail") or "").strip(),
                             job_type="校招", jd_url=detail_url, jd_raw=jd_raw, published_at=str(source.get("publish_date") or row.get("publish_date") or ""),
                             link_kind="detail", campaign_text="软通动力官方校园招聘 API：岗位标题含【27届】且招聘类型为全职")
        job.update({
            "source_job_id": source_id,
            "cohort": 2027,
            "cohort_status": "confirmed",
            "cohort_source": "软通动力官方校园招聘 API",
            "cohort_evidence": "official list title marker 【27届】 + hiretype_name=全职",
            "recruitment_track": "formal",
            "employment_type": str(source.get("hiretype_name") or row.get("hiretype_name") or "").strip(),
            "education": str(source.get("educational_name") or row.get("educational_name") or "").strip(),
            "detail_status": status,
            "jd_status": status,
        })
        return job

    def _hydrate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = []
        for row in rows:
            reason = self._exclude_reason(row)
            if reason:
                self.filtered_counts[reason] += 1
                self.excluded_records.append({"id": str(row.get("id") or ""), "title": str(row.get("name") or ""), "reason": reason})
            else:
                candidates.append(row)
        self.detail_expected_total = len(candidates)
        urls = [self._detail_url(str(row["id"]).strip()) for row in candidates]
        self.detail_unique_urls = len(set(urls))
        jobs: list[dict[str, Any]] = []
        for row, detail_url in zip(candidates, urls):
            source_id = str(row["id"]).strip()
            detail = self._fetch_detail(source_id)
            if detail is not None and str(detail.get("id") or source_id).strip() != source_id:
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_id_mismatch"})
                detail = None
            jd_raw = self._compose_jd(detail) if detail else ""
            if self._complete_jd(jd_raw):
                status = "complete"
                self.detail_count += 1
            else:
                status = "pending"
                reason = "detail_api_empty" if detail is None else "detail_jd_incomplete"
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": reason})
                jd_raw = ""
            jobs.append(self._make_job_record(row, detail, detail_url, jd_raw, status))
        self.retained_count = len(jobs)
        self.detail_complete = self.detail_count == self.detail_expected_total and not self.detail_failures
        self._update_metrics()
        return jobs

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._is_official_url(self.careers_url):
            self.pagination_termination_reason = "non_official_or_wrong_campus_url"
            self._update_metrics()
            return []
        self._warm_session()
        rows = self._list_rows()
        if not self.pagination_complete:
            return []
        return self._hydrate(rows)


SoftstoneCrawler = IsoftstoneCrawler

__all__ = ["IsoftstoneCrawler", "SoftstoneCrawler"]
