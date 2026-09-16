"""Deterministic crawler for OurATS mobile recruitment listings.

OurATS serves a different page to desktop and mobile clients.  The mobile
listing endpoint is the stable source of truth: it exposes a numeric
``settingId`` and a count, while each detail page contains the complete JD.
This crawler deliberately keeps those checks explicit so a partial or mixed
recruitment feed cannot look like a successful crawl.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_JD_SIGNAL_RE = re.compile(
    r"岗位职责|工作职责|职位描述|任职要求|岗位要求|任职资格|工作内容|"
    r"responsibilit|requirement|qualification",
    re.IGNORECASE,
)
_INTERN_TITLE_RE = re.compile(r"实习|intern", re.IGNORECASE)
_SOCIAL_TITLE_RE = re.compile(r"校园大使", re.IGNORECASE)


class OurATSCrawler(BaseCrawler):
    """Crawl a mobile OurATS campus setting with complete-list guarantees.

    ``session`` is injectable for deterministic tests and for callers that
    already manage connection pooling.  A caller serving another OurATS
    tenant can provide its own confirmed campaign evidence; the default
    evidence is only enabled for Skyverse's official host.
    """

    SETTING_ID = "4"
    INTERNSHIP_SETTING_ID = "5"
    MAX_PAGES = 1000
    JD_RAW_LIMIT = 12000
    MOBILE_USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
    LIST_PATH = "/m/req/ajax-get-reqlist/"
    LIST_REFERER_PATH = "/m/req/campus-list"

    # Exact official campaign proof found during the read-only investigation.
    SKYVERSE_CAMPAIGN_EVIDENCE = {
        "cohort": 2027,
        "status": "confirmed",
        "title": "中科飞测2027届校园招聘正式启动",
        "url": "https://mp.weixin.qq.com/s/2lTGCe_qO0eICHgXnDrLzw",
        "published_at": "2026-08-14",
        "start_date": "2026-08-01",
        "scope": "formal_autumn",
    }

    def __init__(
        self,
        company_name: str,
        careers_url: str,
        *,
        session: requests.Session | Any | None = None,
        timeout: int = 30,
        campaign_evidence: dict[str, Any] | None = None,
    ):
        super().__init__(company_name, careers_url)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.origin = self._origin(careers_url)

        host = urlsplit(careers_url).netloc.casefold()
        if campaign_evidence is None and host == "job.skyverse.cn":
            campaign_evidence = dict(self.SKYVERSE_CAMPAIGN_EVIDENCE)
        self.campaign_evidence = dict(campaign_evidence or {})
        self.campaign_confirmed = (
            str(self.campaign_evidence.get("status") or "").casefold()
            == "confirmed"
            and self._as_int(self.campaign_evidence.get("cohort")) == 2027
        )
        self.campaign_start_date = str(
            self.campaign_evidence.get("start_date") or ""
        )
        self.campaign_text = self._campaign_text()

        self.source_rejection_reason = ""
        self.excluded_records: list[dict[str, str]] = []
        self.pagination_page_sizes: list[int] = []
        self.pagination_duplicate_ids: list[str] = []
        self.pagination_expected_total = 0
        self.pagination_pages_fetched = 0
        self.pagination_raw_rows = 0
        self.pagination_unique_ids = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.detail_failures: list[dict[str, str]] = []
        self.detail_duplicate_urls: list[str] = []
        self.detail_expected_total = 0
        self.detail_success_count = 0
        self.detail_unique_urls = 0
        self.detail_complete = False
        self.metrics: dict[str, Any] = {}
        self._update_metrics()

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"OurATS careers URL must be absolute: {url}")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _campaign_text(self) -> str:
        title = str(self.campaign_evidence.get("title") or "").strip()
        url = str(self.campaign_evidence.get("url") or "").strip()
        if title and url:
            return f"{title} | {url}"
        return title or url

    def _update_metrics(self) -> None:
        self.metrics = {
            "source_rejection_reason": self.source_rejection_reason,
            "excluded_count": len(self.excluded_records),
            "excluded_records": list(self.excluded_records),
            "setting_id": self.SETTING_ID,
            "pagination_expected_total": self.pagination_expected_total,
            "pagination_pages_fetched": self.pagination_pages_fetched,
            "pagination_page_sizes": list(self.pagination_page_sizes),
            "pagination_raw_rows": self.pagination_raw_rows,
            "pagination_unique_ids": self.pagination_unique_ids,
            "pagination_duplicate_ids": list(self.pagination_duplicate_ids),
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "detail_expected_total": self.detail_expected_total,
            "detail_success_count": self.detail_success_count,
            "detail_unique_urls": self.detail_unique_urls,
            "detail_failures": list(self.detail_failures),
            "detail_duplicate_urls": list(self.detail_duplicate_urls),
            "detail_complete": self.detail_complete,
            "campaign_cohort": self.campaign_evidence.get("cohort"),
            "campaign_status": self.campaign_evidence.get("status", ""),
            "campaign_confirmed": self.campaign_confirmed,
            "campaign_evidence": self.campaign_text,
        }

    def _source_rejection(self) -> str:
        parsed = urlsplit(self.careers_url)
        path = parsed.path.casefold()
        query = parse_qs(parsed.query)
        setting_values = {str(value) for value in query.get("settingId", [])}

        if path.endswith("/campus.html") or "/campus.html/" in path:
            return "legacy_static_campus_site"
        if self.INTERNSHIP_SETTING_ID in setting_values:
            return "internship_setting_id_5"
        if (
            "/intern-" in path
            or "/intern/" in path
            or path.endswith("/intern")
            or "internship" in path
        ):
            return "internship_source"
        return ""

    def _request(self, url: str, *, api: bool) -> requests.Response:
        headers = {
            "User-Agent": self.MOBILE_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{self.origin}{self.LIST_REFERER_PATH}",
            "Accept": (
                "application/json, text/javascript, */*; q=0.01"
                if api
                else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
        }
        if api:
            headers["X-Requested-With"] = "XMLHttpRequest"
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response

    def _list_url(self, page: int) -> str:
        return (
            f"{self.origin}{self.LIST_PATH}"
            f"?settingId={self.SETTING_ID}&page={page}"
        )

    def _detail_url(self, source_id: str) -> str:
        return (
            f"{self.origin}/m/req/details/"
            f"?settingId={self.SETTING_ID}&id={quote(str(source_id), safe='')}"
        )

    def _fetch_listing_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        expected: int | None = None

        for page in range(1, self.MAX_PAGES + 1):
            try:
                response = self._request(self._list_url(page), api=True)
                payload = response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                self.pagination_termination_reason = f"request_error:{type(exc).__name__}"
                self.pagination_complete = False
                self._update_metrics()
                return rows

            self.pagination_pages_fetched += 1
            if not isinstance(payload, dict):
                self.pagination_termination_reason = "invalid_json_payload"
                self.pagination_complete = False
                self._update_metrics()
                return rows
            if str(payload.get("response") or "").upper() != "SUCCESS":
                self.pagination_termination_reason = "api_response_not_success"
                self.pagination_complete = False
                self._update_metrics()
                return rows

            page_expected = self._as_int(payload.get("count"))
            if page_expected is None or page_expected < 0:
                self.pagination_termination_reason = "invalid_count"
                self.pagination_complete = False
                self._update_metrics()
                return rows
            if expected is None:
                expected = page_expected
            elif page_expected != expected:
                self.pagination_termination_reason = "count_changed_between_pages"
                self.pagination_expected_total = expected
                self.pagination_complete = False
                self._update_metrics()
                return rows

            page_rows = payload.get("data")
            if not isinstance(page_rows, list):
                self.pagination_termination_reason = "invalid_data_payload"
                self.pagination_expected_total = expected
                self.pagination_complete = False
                self._update_metrics()
                return rows
            self.pagination_page_sizes.append(len(page_rows))
            if not page_rows:
                self.pagination_termination_reason = "empty_page"
                break

            for row in page_rows:
                if not isinstance(row, dict):
                    self.pagination_termination_reason = "invalid_row_payload"
                    self.pagination_expected_total = expected
                    self.pagination_complete = False
                    self._update_metrics()
                    return rows
                source_id = str(row.get("requisition_id") or "").strip()
                if not source_id:
                    self.pagination_termination_reason = "row_id_missing"
                    self.pagination_expected_total = expected
                    self.pagination_complete = False
                    self._update_metrics()
                    return rows
                if source_id in seen_ids:
                    self.pagination_duplicate_ids.append(source_id)
                else:
                    seen_ids.add(source_id)
                rows.append(row)
        else:
            self.pagination_termination_reason = "max_pages"

        self.pagination_expected_total = expected or 0
        self.pagination_raw_rows = len(rows)
        self.pagination_unique_ids = len(seen_ids)
        self.pagination_complete = bool(
            self.pagination_termination_reason == "empty_page"
            and expected is not None
            and len(rows) == expected
            and len(seen_ids) == expected
            and not self.pagination_duplicate_ids
        )
        if not self.pagination_complete and self.pagination_termination_reason == "empty_page":
            if self.pagination_duplicate_ids:
                self.pagination_termination_reason = "duplicate_ids"
            elif len(rows) != self.pagination_expected_total:
                self.pagination_termination_reason = "row_count_mismatch"
            else:
                self.pagination_termination_reason = "unique_id_count_mismatch"
        self._update_metrics()
        return rows

    @staticmethod
    def _record_exclusion(row: dict[str, Any]) -> str:
        setting_id = str(row.get("setting_id") or row.get("settingId") or "").strip()
        if setting_id and setting_id != "4":
            return f"setting_id_{setting_id}"

        title = str(row.get("req_full_name") or row.get("title") or "").strip()
        if _SOCIAL_TITLE_RE.search(title):
            return "campus_ambassador"
        if _INTERN_TITLE_RE.search(title):
            return "internship_title"

        schedule = str(row.get("work_schedule") or "").strip()
        if schedule != "全职":
            return "non_full_time"
        return ""

    @staticmethod
    def _city(row: dict[str, Any]) -> str:
        city = str(row.get("city") or "").strip()
        if city:
            return city
        location = str(row.get("location") or "").strip()
        return location

    def _is_current_campaign_record(self, row: dict[str, Any]) -> bool:
        if not self.campaign_confirmed or not self.campaign_start_date:
            return False
        title = str(row.get("req_full_name") or row.get("title") or "").strip()
        schedule = str(row.get("work_schedule") or "").strip()
        published_at = str(row.get("company_website_post_date") or "").strip()
        return (
            "校招" in title
            and schedule == "全职"
            and bool(re.match(r"^20\d{2}-\d{2}-\d{2}", published_at))
            and published_at[:10] >= self.campaign_start_date[:10]
        )

    @staticmethod
    def _normalize_detail_text(text: str) -> str:
        lines = [" ".join(line.split()) for line in (text or "").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @classmethod
    def _parse_jd(cls, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        candidates: list[str] = []
        selectors = (
            "[class*='content-']",
            "[class*='job-details-']",
            ".job-details",
            "[class~='content']",
        )
        for selector in selectors:
            for node in soup.select(selector):
                text = cls._normalize_detail_text(node.get_text("\n", strip=True))
                if 60 <= len(text) <= cls.JD_RAW_LIMIT and _JD_SIGNAL_RE.search(text):
                    candidates.append(text)
        if candidates:
            return min(candidates, key=len)[: cls.JD_RAW_LIMIT]

        # Keep a bounded fallback for OurATS template variants that do not use
        # the hashed content class but still expose a semantic JD heading.
        for marker in soup.find_all(string=lambda value: _JD_SIGNAL_RE.search(str(value or ""))):
            parent = marker.parent
            for _ in range(10):
                if parent is None:
                    break
                text = cls._normalize_detail_text(parent.get_text("\n", strip=True))
                if 60 <= len(text) <= cls.JD_RAW_LIMIT and _JD_SIGNAL_RE.search(text):
                    return text[: cls.JD_RAW_LIMIT]
                parent = parent.parent
        return ""

    def _fetch_detail(self, source_id: str, url: str) -> str:
        response = self._request(url, api=False)
        jd_raw = self._parse_jd(response.text)
        if not jd_raw:
            raise ValueError("jd_not_found")
        return jd_raw

    def _normalize_job(
        self,
        row: dict[str, Any],
        detail_url: str,
        jd_raw: str,
    ) -> dict[str, Any]:
        source_id = str(row.get("requisition_id") or "").strip()
        job = self._make_job(
            title=str(row.get("req_full_name") or row.get("title") or "").strip(),
            city=self._city(row),
            job_type="校招",
            jd_url=detail_url,
            jd_raw=jd_raw,
            published_at=str(row.get("company_website_post_date") or "").strip(),
            link_kind="detail",
            campaign_text=self.campaign_text if self._is_current_campaign_record(row) else "",
        )
        job.update(
            {
                "source_job_id": source_id,
                "source_setting_id": int(self.SETTING_ID),
                "source_platform": "OurATS",
            }
        )
        if self._is_current_campaign_record(row):
            job.update(
                {
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "cohort_source": "官方招聘项目",
                    "cohort_evidence": self.campaign_text,
                    "recruitment_track": "formal",
                    "campaign_scope": "formal_autumn",
                }
            )
        return job

    def fetch(self) -> list[dict]:
        self.source_rejection_reason = self._source_rejection()
        if self.source_rejection_reason:
            self.pagination_termination_reason = self.source_rejection_reason
            self._update_metrics()
            return []

        rows = self._fetch_listing_rows()
        if not self.pagination_complete:
            self._update_metrics()
            return []

        accepted_rows: list[dict[str, Any]] = []
        for row in rows:
            reason = self._record_exclusion(row)
            if reason:
                source_id = str(row.get("requisition_id") or "").strip()
                self.excluded_records.append({"id": source_id, "reason": reason})
                continue
            accepted_rows.append(row)

        self.detail_expected_total = len(accepted_rows)
        jobs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row in accepted_rows:
            source_id = str(row.get("requisition_id") or "").strip()
            detail_url = self._detail_url(source_id)
            if detail_url in seen_urls:
                self.detail_duplicate_urls.append(detail_url)
                continue
            seen_urls.add(detail_url)
            try:
                jd_raw = self._fetch_detail(source_id, detail_url)
            except (requests.RequestException, ValueError, TypeError) as exc:
                self.detail_failures.append(
                    {
                        "id": source_id,
                        "url": detail_url,
                        "reason": str(exc) or type(exc).__name__,
                    }
                )
                continue
            self.detail_success_count += 1
            jobs.append(self._normalize_job(row, detail_url, jd_raw))

        self.detail_unique_urls = len(seen_urls)
        self.detail_complete = bool(
            self.detail_success_count == self.detail_expected_total
            and self.detail_unique_urls == self.detail_expected_total
            and not self.detail_failures
            and not self.detail_duplicate_urls
        )
        self._update_metrics()
        return jobs if self.detail_complete else []


__all__ = ["OurATSCrawler"]
