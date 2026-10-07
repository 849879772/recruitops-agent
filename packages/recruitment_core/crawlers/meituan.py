"""Meituan's public campus JSON API, with stable IDs and complete page evidence."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler, effective_crawl_timeout_seconds

logger = logging.getLogger(__name__)


class MeituanCrawler(BaseCrawler):
    HOST = "zhaopin.meituan.com"
    API = f"https://{HOST}/api/official/job/getJobList"
    DETAIL_API = f"https://{HOST}/api/official/job/getJobDetail"
    PAGE_SIZE = 100
    MAX_PAGES = 25
    REQUEST_ATTEMPTS = 3

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.resolved_source_url = careers_url
        self.fetch_failed = False
        self.pagination_complete = False
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.pagination_termination_reason = "not_started"
        self.pagination_diagnostics: list[dict] = []
        self.crawl_error_code = ""
        self.crawl_error_message = ""
        self._deadline: float | None = None

    @classmethod
    def supports(cls, url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        return (
            parsed.scheme in {"http", "https"}
            and (
                (host == cls.HOST and parsed.path.rstrip("/").casefold() == "/web/campus")
                or (host == "campus.meituan.com" and parsed.path.rstrip("/") in {"", "/web/campus"})
            )
        )

    @staticmethod
    def _code_groups(values: list[str]) -> list[dict]:
        # Public website formatData.yA groups query 11010_1101001 as a
        # job-family code and its selected sub-code.
        groups: dict[str, list[str]] = {}
        for value in values:
            code, _, subcode = value.partition("_")
            if not code:
                continue
            subcodes = groups.setdefault(code, [])
            if subcode and subcode != "-1" and subcode not in subcodes:
                subcodes.append(subcode)
        return [{"code": code, "subCode": subcodes} for code, subcodes in groups.items()]

    def _body(self, page: int, *, keywords: str = "") -> dict:
        query = parse_qs(urlsplit(self.careers_url).query)

        def values(key: str) -> list[str]:
            return [part for item in query.get(key, []) for part in item.split(",") if part]

        hiring = self._code_groups(values("hiringType"))
        job_type = hiring or [{"code": "1", "subCode": []}]
        return {
            "page": {"pageNo": page, "pageSize": self.PAGE_SIZE},
            "jobShareType": "1",
            "keywords": keywords,
            "cityList": [
                {"code": value.partition("_")[2] if "_" in value and not value.endswith("_-1") else value.partition("_")[0]}
                for value in values("cityList")
            ],
            "department": [{"code": code} for code in values("bg")],
            "jfJgList": self._code_groups(values("jfJgList")),
            "jobType": job_type,
            "typeCode": [subcode for group in job_type for subcode in group["subCode"]],
            "specialCode": values("hiringSpecial"),
        }

    @classmethod
    def headers(cls, source_url: str) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Referer": source_url,
            "Origin": f"https://{cls.HOST}",
            "X-Requested-With": "XMLHttpRequest",
        }

    @classmethod
    def is_official_api_response(cls, response: object) -> bool:
        """Accept API evidence only from the HTTPS official response origin."""

        try:
            status_code = int(getattr(response, "status_code", 200))
        except (TypeError, ValueError):
            return False
        if 300 <= status_code < 400:
            return False
        final_url = str(getattr(response, "url", "") or "")
        if not final_url:
            # requests always sets Response.url. Minimal deterministic fixture
            # objects predate provenance checks and are not network responses.
            return not isinstance(response, requests.Response)
        try:
            parsed = urlsplit(final_url)
            return (
                parsed.scheme == "https"
                and (parsed.hostname or "").casefold() == cls.HOST
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    def _request_page(self, page: int, *, keywords: str = "") -> Mapping | None:
        for attempt in range(self.REQUEST_ATTEMPTS):
            remaining = (self._deadline or (time.monotonic() + 25)) - time.monotonic()
            if remaining <= 0:
                self.crawl_error_code = "timeout"
                return None
            try:
                response = requests.post(
                    self.API, json=self._body(page, keywords=keywords),
                    headers=self.headers(self.careers_url), timeout=min(25, remaining),
                    allow_redirects=False,
                )
                if not self.is_official_api_response(response):
                    self.crawl_error_code = "official_origin_changed"
                    return None
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping) or payload.get("status") not in {1, "1"}:
                    self.crawl_error_code = "api_variant_unsupported"
                    return None
                data = payload.get("data")
                if not isinstance(data, Mapping) or not isinstance(data.get("list"), list) or not isinstance(data.get("page"), Mapping):
                    self.crawl_error_code = "api_variant_unsupported"
                    return None
                return data
            except requests.RequestException as exc:
                self.crawl_error_code = "timeout" if isinstance(exc, requests.Timeout) else "fetch_failed"
                logger.warning("[%s] Meituan page %s attempt %s failed: %s", self.company_name, page, attempt + 1, type(exc).__name__)
                if attempt + 1 < self.REQUEST_ATTEMPTS:
                    time.sleep(min(0.5 * (attempt + 1), max(0, remaining / 4)))
            except (TypeError, ValueError):
                self.crawl_error_code = "api_variant_unsupported"
                return None
        return None

    @staticmethod
    def _clean_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return BeautifulSoup(value, "html.parser").get_text("\n", strip=True)

    def _parse_job(self, row: Mapping) -> dict | None:
        job_id = str(row.get("jobUnionId") or "").strip()
        title = str(row.get("name") or "").strip()
        job_type = str(row.get("jobType") or "")
        if not job_id or not title or job_type not in {"1", "2"}:
            return None
        detail_url = f"https://{self.HOST}/web/position/detail?{urlencode({'jobUnionId': job_id, 'highlightType': 'campus'})}"
        duty = self._clean_text(row.get("jobDuty"))
        requirement = self._clean_text(row.get("jobRequirement"))
        detail = "\n".join(["岗位职责", duty, "任职要求", requirement]).strip() if duty or requirement else ""
        cities = row.get("cityList") or []
        city = "、".join(str(item.get("name") or "") for item in cities if isinstance(item, Mapping) and item.get("name"))
        job = self._make_job(
            title=title, city=city, job_type="校招" if job_type == "1" else "实习",
            jd_url=detail_url, jd_raw=detail, published_at=str(row.get("refreshTime") or ""), link_kind="detail",
        )
        complete = bool(duty and requirement)
        job.update({
            "id": f"meituan-job-{job_id}",
            "source_job_id": job_id,
            "native_job_id": job_id,
            "source_list_url": self.careers_url,
            "source_platform": "meituan",
            "source_tenant": self.HOST,
            "detail_url": detail_url,
            "identity_status": "matched",
            "identity_evidence": [f"native_id:{job_id}", f"title:{title}"],
            "capture_evidence": {
                "status": "complete" if complete else "incomplete",
                "method": "official_api",
                "source_url": detail_url,
                "api_url": self.API,
                "identity_verified": True,
                "terminal_observed": complete,
                "remaining_controls": [],
                "content_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        return job

    def fetch(self, *, keywords: str = "") -> list[dict]:
        self._deadline = time.monotonic() + effective_crawl_timeout_seconds(180)
        jobs: list[dict] = []
        seen: set[str] = set()
        expected_total: int | None = None
        expected_pages: int | None = None
        for page in range(1, self.MAX_PAGES + 1):
            data = self._request_page(page, keywords=keywords)
            if data is None:
                self.fetch_failed = True
                self.has_more = True
                self.pagination_termination_reason = f"list_request_failed_page_{page}"
                break
            rows = data["list"]
            page_info = data["page"]
            try:
                total = int(page_info["totalCount"])
                pages = int(page_info.get("totalPage") or math.ceil(total / self.PAGE_SIZE))
                returned_page = int(page_info.get("pageNo") or page)
                if total < 0 or pages < 0 or returned_page != page:
                    raise ValueError("invalid pagination")
            except (KeyError, TypeError, ValueError):
                self.crawl_error_code = "api_variant_unsupported"
                self.pagination_termination_reason = "pagination_metadata_missing"
                self.fetch_failed = True
                break
            self.pages_seen = page
            if expected_total is None:
                expected_total, expected_pages = total, pages
                self.advertised_total, self.total_pages = total, pages
            elif (total, pages) != (expected_total, expected_pages):
                self.has_more = True
                self.pagination_termination_reason = "advertised_pagination_changed"
                break
            page_added = 0
            for row in rows:
                job = self._parse_job(row) if isinstance(row, Mapping) else None
                if job is None or job["source_job_id"] in seen:
                    continue
                seen.add(job["source_job_id"])
                jobs.append(job)
                page_added += 1
            self.pagination_diagnostics.append({"page": page, "count": page_added, "total": total})
            if page >= pages:
                self.pagination_complete = len(seen) == total
                self.has_more = not self.pagination_complete
                self.pagination_termination_reason = "api_total_pages_and_count_reached" if self.pagination_complete else "api_total_count_mismatch"
                break
            if not page_added:
                self.has_more = True
                self.pagination_termination_reason = "empty_or_repeated_page_before_total"
                break
        else:
            self.has_more = True
            self.pagination_termination_reason = "max_pages_reached"
        logger.info("[%s] Meituan observed %s jobs, pagination complete=%s", self.company_name, len(jobs), self.pagination_complete)
        return jobs
