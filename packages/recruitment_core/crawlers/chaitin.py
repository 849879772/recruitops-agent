"""Deterministic WhaleHire crawler for Chaitin's official campus site.

The public WhaleHire site exposes a tenant-scoped JSON API.  This crawler is
deliberately strict: it only returns full-time campus jobs whose own title or
JD contains an explicit 2027 cohort marker, and it fails closed when either
pagination or detail hydration cannot be proven complete.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


_INTERNSHIP_RE = re.compile(
    r"实习生|实习招聘|日常实习|应届实习|实习专项|intern(?:ship)?",
    re.IGNORECASE,
)
_NON_FORMAL_RE = re.compile(
    r"提前批|提前招聘|提前选拔|社会招聘|社招|往届|兼职|劳务",
    re.IGNORECASE,
)
_COHORT_RE = re.compile(r"(?<!\d)(20\d{2}|2[0-9])\s*(?:届|年)")
_COHORT_CONTEXT_RE = re.compile(
    r"(?<!\d)(20\d{2}|2[0-9])\s*(?:届|年)\s*(?:校招|校园招聘|秋招|春招|应届)?",
    re.IGNORECASE,
)
_DUTY_RE = re.compile(
    r"岗位职责|职位描述|工作职责|岗位描述|岗位简介|岗位介绍|职位介绍|工作内容|岗位概述",
    re.IGNORECASE,
)
_REQUIREMENT_RE = re.compile(
    r"任职要求|职位要求|岗位要求|任职资格|学历要求",
    re.IGNORECASE,
)


class ChaitinCrawler(BaseCrawler):
    """Fetch Chaitin's WhaleHire campus jobs through its public JSON API."""

    OFFICIAL_HOSTS = {"join.chaitin.cn"}
    PAGE_SIZE = 10
    MAX_PAGES = 100
    REQUEST_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 0.25
    DETAIL_REQUEST_DELAY_SECONDS = 0.2
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

        self.detail_api_calls = 0
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
            "api_calls": self.detail_api_calls,
            "expected_total": self.detail_expected_total,
            "count": self.detail_count,
            "unique_urls": self.detail_unique_urls,
            "complete": self.detail_complete,
            "failures": list(self.detail_failures),
        }
        self.metrics = {
            "pagination_complete": self.pagination_complete,
            "detail_complete": self.detail_complete,
            "filtered_internship_count": self.filtered_internship_count,
            "excluded_count": len(self.excluded_records),
            "pagination": pagination,
            "detail": detail,
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return dict(self.metrics["pagination"])

    def detail_metrics(self) -> dict[str, Any]:
        return dict(self.metrics["detail"])

    @staticmethod
    def _site_base(url: str) -> str:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _api_base(self) -> str:
        parsed = urlsplit(self.careers_url)
        path = parsed.path.rstrip("/")
        marker = "/sites/"
        if parsed.netloc.casefold() not in self.OFFICIAL_HOSTS or marker not in path:
            return ""
        prefix, tenant = path.split(marker, 1)
        tenant = tenant.strip("/").split("/", 1)[0]
        if not prefix or not tenant:
            return ""
        return urlunsplit(
            (parsed.scheme, parsed.netloc, f"{prefix}/api/{tenant}/jobs", "", "")
        )

    def _detail_url(self, job_id: str) -> str:
        return f"{self._site_base(self.careers_url)}/jobs/{quote(job_id, safe='')}"

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.careers_url,
        }

    def _get_json(self, url: str, *, params: dict[str, object] | None = None) -> dict[str, Any] | None:
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            response = None
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("code") not in (0, "0", None):
                    raise ValueError("invalid_whalehire_payload")
                return payload
            except (requests.RequestException, ValueError, TypeError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] WhaleHire API 请求失败 %s: %s",
                        self.company_name,
                        url,
                        exc,
                    )
                    return None
                delay = self.RETRY_BACKOFF_SECONDS * attempt
                if getattr(response, "status_code", None) == 429:
                    retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        delay = max(delay, float(attempt))
                time.sleep(delay)
        return None

    @staticmethod
    def _data(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    @staticmethod
    def _job_text(row: dict[str, Any]) -> str:
        return "\n".join(
            str(row.get(key) or "").strip()
            for key in ("title", "description", "job_category_name", "department")
        )

    @classmethod
    def _cohort_markers(cls, text: str) -> list[str]:
        return [match.group(0).strip() for match in _COHORT_CONTEXT_RE.finditer(text or "")]

    @classmethod
    def _has_explicit_2027_evidence(cls, text: str) -> bool:
        return bool(re.search(r"(?<!\d)(?:2027|27)\s*(?:届|年)", text or ""))

    @classmethod
    def _has_conflicting_cohort(cls, text: str) -> bool:
        for marker in cls._cohort_markers(text):
            normalized = re.sub(r"\s+", "", marker)
            if not normalized.startswith(("2027", "27")):
                return True
        return False

    @classmethod
    def _is_internship(cls, row: dict[str, Any]) -> bool:
        work_type = str(row.get("work_type") or "").strip().casefold()
        return work_type in {"internship", "intern", "实习"} or bool(
            _INTERNSHIP_RE.search(cls._job_text(row))
        )

    @classmethod
    def _is_non_formal(cls, row: dict[str, Any]) -> bool:
        return bool(_NON_FORMAL_RE.search(cls._job_text(row)))

    @classmethod
    def _complete_jd(cls, text: str) -> bool:
        normalized = str(text or "").strip()
        requirement_match = _REQUIREMENT_RE.search(normalized)
        has_description_prefix = bool(
            requirement_match and len(normalized[: requirement_match.start()].strip()) >= 20
        )
        return bool(
            normalized
            and len(normalized) >= 20
            and requirement_match
            and (_DUTY_RE.search(normalized) or has_description_prefix)
        )

    @staticmethod
    def _clean_text(value: object) -> str:
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    def _list_rows(self) -> list[dict[str, Any]]:
        api = self._api_base()
        if not api:
            self.pagination_termination_reason = "invalid_whalehire_site_url"
            self._update_metrics()
            return []

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page in range(1, self.MAX_PAGES + 1):
            payload = self._get_json(
                api,
                params={"page": page, "size": self.PAGE_SIZE, "recruitment_type": "campus"},
            )
            data = self._data(payload)
            items = data.get("items") if data else None
            if not isinstance(items, list):
                self.pagination_termination_reason = f"list_payload_missing_items_page_{page}"
                self._update_metrics()
                return []

            self.pages_fetched = page
            self.page_sizes.append(len(items))
            self.raw_listed_count += len(items)
            page_ids: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    self.pagination_termination_reason = f"invalid_item_page_{page}"
                    self._update_metrics()
                    return []
                source_id = str(item.get("job_id") or "").strip()
                if not source_id:
                    self.pagination_termination_reason = f"job_id_missing_page_{page}"
                    self._update_metrics()
                    return []
                if source_id in page_ids or source_id in seen_ids:
                    self.pagination_duplicate_ids.append(source_id)
                    continue
                page_ids.add(source_id)
                seen_ids.add(source_id)
                rows.append(item)

            total = data.get("total_count")
            try:
                parsed_total = int(total)
            except (TypeError, ValueError):
                self.pagination_termination_reason = "total_count_missing"
                self._update_metrics()
                return []
            if self.expected_total is None:
                self.expected_total = parsed_total
            elif self.expected_total != parsed_total:
                self.pagination_termination_reason = "total_count_changed"
                self._update_metrics()
                return []

            has_next = bool(data.get("has_next_page"))
            if not has_next:
                self.unique_listed_count = len(seen_ids)
                self.pagination_complete = self.unique_listed_count == self.expected_total
                self.pagination_termination_reason = (
                    "expected_total_reached" if self.pagination_complete else "row_count_mismatch"
                )
                self._update_metrics()
                return rows if self.pagination_complete else []
            if not items or not page_ids:
                self.pagination_termination_reason = "next_page_without_new_rows"
                self._update_metrics()
                return []
        self.pagination_termination_reason = "max_pages"
        self.unique_listed_count = len(seen_ids)
        self._update_metrics()
        return []

    def _detail_job(self, source_id: str) -> dict[str, Any] | None:
        api = self._api_base()
        payload = self._get_json(f"{api}/{quote(source_id, safe='')}") if api else None
        data = self._data(payload)
        job = data.get("job") if data else None
        return job if isinstance(job, dict) else None

    def _make_confirmed_job(self, detail: dict[str, Any], jd_raw: str, evidence: str) -> dict[str, Any]:
        source_id = str(detail.get("job_id") or "").strip()
        job = self._make_job(
            title=str(detail.get("title") or "").strip(),
            city=str(detail.get("location") or "").strip(),
            job_type="校招",
            jd_url=self._detail_url(source_id),
            jd_raw=jd_raw[: self.JD_RAW_LIMIT],
            link_kind="detail",
            campaign_text=evidence,
        )
        job.update(
            {
                "source_job_id": source_id,
                "tracking_code": str(detail.get("tracking_code") or "").strip(),
                "department": str(detail.get("department") or "").strip(),
                "job_category_name": str(detail.get("job_category_name") or "").strip(),
                "work_type": str(detail.get("work_type") or "").strip(),
                "recruitment_type": str(detail.get("recruitment_type") or "").strip(),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "cohort_source": "长亭科技官方 WhaleHire 岗位 API",
                "cohort_evidence": evidence,
                "recruitment_track": "formal",
            }
        )
        return job

    def _hydrate_details(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            text = self._job_text(row)
            if self._is_internship(row):
                self.filtered_internship_count += 1
                self.excluded_records.append({"id": str(row.get("job_id") or ""), "title": title, "reason": "internship"})
                continue
            if self._is_non_formal(row):
                self.excluded_records.append({"id": str(row.get("job_id") or ""), "title": title, "reason": "non_formal"})
                continue
            if self._has_conflicting_cohort(text):
                self.excluded_records.append({"id": str(row.get("job_id") or ""), "title": title, "reason": "conflicting_cohort_evidence"})
                continue
            if not self._has_explicit_2027_evidence(text):
                self.excluded_records.append({"id": str(row.get("job_id") or ""), "title": title, "reason": "missing_explicit_2027_evidence"})
                continue
            evidence = next(
                (marker for marker in self._cohort_markers(text) if re.match(r"(?:2027|27)", re.sub(r"\s+", "", marker))),
                "2027届",
            )
            candidates.append((row, evidence))

        self.detail_expected_total = len(candidates)
        jobs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row, evidence in candidates:
            source_id = str(row.get("job_id") or "").strip()
            if self.detail_api_calls:
                time.sleep(self.DETAIL_REQUEST_DELAY_SECONDS)
            self.detail_api_calls += 1
            detail = self._detail_job(source_id)
            detail_url = self._detail_url(source_id)
            self.detail_unique_urls = len(seen_urls | {detail_url})
            if detail is None:
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_missing"})
                continue
            if str(detail.get("job_id") or "").strip() != source_id:
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_id_mismatch"})
                continue
            detail_text = self._job_text(detail)
            if self._is_internship(detail) or self._is_non_formal(detail):
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_non_formal_or_internship"})
                continue
            if self._has_conflicting_cohort(detail_text) or not self._has_explicit_2027_evidence(detail_text):
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_cohort_evidence_missing_or_conflicting"})
                continue
            jd_raw = self._clean_text(detail.get("description") or row.get("description"))
            if not self._complete_jd(jd_raw):
                self.detail_failures.append({"id": source_id, "url": detail_url, "reason": "detail_jd_incomplete"})
                continue
            seen_urls.add(detail_url)
            self.detail_count += 1
            jobs.append(self._make_confirmed_job(detail, jd_raw, evidence))

        self.detail_unique_urls = len(seen_urls | {
            self._detail_url(str(row.get("job_id") or "").strip()) for row, _ in candidates
        }) if candidates else 0
        self.detail_complete = self.detail_count == self.detail_expected_total and not self.detail_failures
        self._update_metrics()
        return jobs

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        rows = self._list_rows()
        if not self.pagination_complete:
            return []
        jobs = self._hydrate_details(rows)
        return jobs if self.detail_complete else []


__all__ = ["ChaitinCrawler"]
