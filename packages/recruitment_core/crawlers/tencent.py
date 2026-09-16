"""Read-only crawler for Tencent's public campus recruitment site.

The public site is a single-page application. Its post list is backed by
``/api/v1/position/searchPosition`` and its detail pages by
``/api/v1/jobDetails/getJobDetailsByPostId``. This adapter deliberately uses
those read-only endpoints instead of scraping rendered cards or attempting a
login flow.
"""

from __future__ import annotations

import html
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler


logger = logging.getLogger(__name__)


class TencentCrawler(BaseCrawler):
    """Fetch current Tencent campus positions from the official public APIs."""

    ORIGIN = "https://join.qq.com"
    LIST_API = f"{ORIGIN}/api/v1/position/searchPosition"
    API = LIST_API
    PROJECT_MAPPING_API = f"{ORIGIN}/api/v1/position/getProjectMapping"
    DETAIL_API = f"{ORIGIN}/api/v1/jobDetails/getJobDetailsByPostId"
    DETAIL_URL_TEMPLATE = f"{ORIGIN}/post_detail.html?postid={{post_id}}"

    # The frontend accepts larger pages than the UI default of five. Keeping
    # this explicit makes the pagination proof independent of the UI page size.
    PAGE_SIZE = 1000
    MAX_PAGES = 100
    CURRENT_COHORT = 2027
    DEFAULT_FORMAL_MAPPING_IDS = (1, 14, 9)
    REQUEST_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 0.4
    DETAIL_WORKERS = 16
    RECRUIT_TYPE = "40003"  # Kept as a compatibility constant; not sent.

    def __init__(self, company: str, careers_url: str) -> None:
        super().__init__(company, careers_url)
        self.session = requests.Session()
        self._reset_state()

    def _reset_state(self) -> None:
        """Reset observable evidence so one crawler instance is reusable."""

        self.pages_seen = 0
        self.pages_fetched = 0
        self.page_count = 0
        self.total_pages: int | None = None
        self.expected_pages: int | None = None
        self.advertised_total: int | None = None
        self.expected_total: int | None = None
        self.total_count: int | None = None
        self.raw_listed_count = 0
        self.listed_count = 0
        self.unique_listed_count = 0
        self.filtered_internship_count = 0
        self.has_more = False
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []

        self.fetch_failed = False
        self.resolved_source_url = ""
        self.source_kind = "unknown"
        self.requested_filters: dict[str, Any] = {}
        self.selected_project_ids: list[int] = []
        self.requested_project_ids: list[int] = []
        self.excluded_project_ids: list[int] = []
        self.project_mapping_request_failed = False
        self.campaign_validated = False
        self.campaign_evidence = ""
        self.cohort = self.CURRENT_COHORT
        self.cohort_status = "unconfirmed"
        self.cohort_source = f"{self.PROJECT_MAPPING_API}"
        self.cohort_evidence = ""
        self.campaign_text = f"腾讯{self.CURRENT_COHORT}校园招聘"

        self.share_id = ""
        self.share_requires_login = False
        self.share_scope = ""
        self.request_log: list[dict[str, str]] = []
        self.adapter_complete = False
        self.metrics: dict[str, Any] = {}
        self.completeness_evidence: dict[str, Any] = {}

    @staticmethod
    def _compact(value: Any) -> str:
        if value is None:
            return ""
        return " ".join(str(value).replace("\xa0", " ").split())

    @classmethod
    def _plain_text(cls, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).replace("\r", "")
        soup = BeautifulSoup(text, "html.parser")
        lines = [cls._compact(line) for line in soup.get_text("\n").splitlines()]
        return "\n".join(line for line in lines if line)

    @classmethod
    def _city_text(cls, value: Any) -> str:
        if isinstance(value, Mapping):
            for key in ("name", "cityName", "label", "value"):
                text = cls._compact(value.get(key))
                if text:
                    return text
            return ""
        if isinstance(value, (list, tuple)):
            parts = [cls._city_text(item) for item in value]
            return "、".join(part for part in parts if part)
        return cls._compact(value)

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @classmethod
    def _first_query_value(cls, query: Mapping[str, list[str]], key: str) -> str:
        values = query.get(key, [])
        return cls._compact(values[0]) if values else ""

    @classmethod
    def _extract_share_id(cls, value: Any) -> str:
        """Extract shareId from normal or mangled login redirect state."""

        candidate = cls._compact(value)
        for _ in range(3):
            decoded = html.unescape(unquote(candidate))
            if decoded == candidate:
                break
            candidate = decoded

        patterns = (
            r"shareid\s*(?:[:=]|%3a|%3d)?\s*(\d+)",
            r"share(?:\.html|html)[^0-9]{0,40}(\d{6,})",
        )
        for pattern in patterns:
            match = re.search(pattern, candidate, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _parse_filter_ids(
        cls,
        query_value: str,
    ) -> tuple[list[int], list[int], list[int], list[int], list[int]]:
        project_ids: list[int] = []
        bg_ids: list[int] = []
        work_city_ids: list[int] = []
        recruit_city_ids: list[int] = []
        position_family_ids: list[int] = []
        seen: set[tuple[str, int]] = set()

        for raw_token in query_value.split(","):
            token = cls._compact(unquote(raw_token))
            match = re.fullmatch(r"([pbwr]|[2-7])_([0-9]+)", token, flags=re.IGNORECASE)
            if not match:
                continue
            prefix = match.group(1).lower()
            value = int(match.group(2))
            marker = (prefix, value)
            if marker in seen:
                continue
            seen.add(marker)
            if prefix == "p":
                project_ids.append(value)
            elif prefix == "b":
                bg_ids.append(value)
            elif prefix == "w":
                work_city_ids.append(value)
            elif prefix == "r":
                recruit_city_ids.append(value)
            else:
                position_family_ids.append(value)

        return project_ids, bg_ids, work_city_ids, recruit_city_ids, position_family_ids

    @classmethod
    def parse_source_url(cls, source_url: str) -> dict[str, Any]:
        """Parse homepage, post query, and login/share URL forms.

        Some evaluation URLs contain literal ``&amp;`` and a shortened
        ``state=httpsjoin.qq.commshare.htmlshareId...`` value. Decode both
        forms before looking at the query so they have the same scope as the
        normal browser URL.
        """

        original = cls._compact(source_url)
        normalized = html.unescape(original)
        parsed = urlsplit(normalized)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_value = cls._first_query_value(query, "query")
        (
            project_ids,
            bg_ids,
            work_city_ids,
            recruit_city_ids,
            position_family_ids,
        ) = cls._parse_filter_ids(query_value)

        state = cls._first_query_value(query, "state")
        share_id = cls._extract_share_id(state)
        if not share_id:
            share_id = cls._extract_share_id(normalized)

        path = (parsed.path or "/").lower().rstrip("/") or "/"
        if share_id and path == "/login.html":
            source_kind = "login_share"
        elif path == "/share.html":
            source_kind = "share"
        elif path == "/post.html":
            source_kind = "post_query" if query_value else "post_home"
        elif path == "/":
            source_kind = "home"
        else:
            source_kind = "unsupported"

        work_country_type = cls._as_int(cls._first_query_value(query, "c_t")) or 0
        return {
            "original_url": original,
            "normalized_url": normalized,
            "host": parsed.netloc.lower(),
            "path": path,
            "source_kind": source_kind,
            "query_value": query_value,
            "keyword": cls._first_query_value(query, "keyword"),
            "project_ids": project_ids,
            "bg_ids": bg_ids,
            "work_city_ids": work_city_ids,
            "recruit_city_ids": recruit_city_ids,
            "position_family_ids": position_family_ids,
            "work_country_type": work_country_type,
            "foreign_city_scope": cls._first_query_value(query, "f_c"),
            "share_id": share_id,
            "share_requires_login": bool(share_id and source_kind in {"login_share", "share"}),
            "share_url": f"{cls.ORIGIN}/share.html?shareId={share_id}" if share_id else "",
        }

    @classmethod
    def _parse_source_url(cls, source_url: str) -> dict[str, Any]:
        """Backward-compatible private alias used by crawler tests."""

        return cls.parse_source_url(source_url)

    @staticmethod
    def _is_success(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        for key in ("status", "code"):
            if key not in payload:
                continue
            value = payload.get(key)
            if value not in (None, 0, "0", 200, "200"):
                return False
        return True

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (compatible; RecruitmentCrawler/1.0)",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _post_json(self, url: str, body: dict[str, Any], referer: str | None = None) -> Any:
        self.request_log.append({"method": "POST", "url": url})
        for attempt in range(self.REQUEST_ATTEMPTS):
            try:
                response = self.session.post(
                    url,
                    json=body,
                    headers={**self._headers(referer), "Content-Type": "application/json"},
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 == self.REQUEST_ATTEMPTS:
                    logger.warning("Tencent POST failed: %s (%s)", url, exc)
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * (attempt + 1))
        return None

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> Any:
        self.request_log.append({"method": "GET", "url": url})
        for attempt in range(self.REQUEST_ATTEMPTS):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(referer),
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 == self.REQUEST_ATTEMPTS:
                    logger.warning("Tencent GET failed: %s (%s)", url, exc)
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * (attempt + 1))
        return None

    @classmethod
    def _mapping_entries(cls, payload: Any) -> list[dict[str, Any]]:
        if not cls._is_success(payload):
            return []
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            return []

        entries: list[dict[str, Any]] = []
        for group in data:
            if not isinstance(group, Mapping):
                continue
            subprojects = group.get("subProjectList") or group.get("subProjects") or []
            if not isinstance(subprojects, list):
                continue
            for item in subprojects:
                if not isinstance(item, Mapping):
                    continue
                mapping_id = cls._as_int(item.get("mappingId") or item.get("id"))
                if mapping_id is None:
                    continue
                project_name = cls._compact(item.get("projectName") or item.get("name"))
                label = cls._compact(item.get("recruitLabelName") or item.get("label"))
                year = cls._compact(item.get("recruitYear") or item.get("year"))
                text = f"{project_name} {label}"
                entries.append(
                    {
                        "mapping_id": mapping_id,
                        "project_name": project_name,
                        "label": label,
                        "year": year,
                        "is_current_formal": year == str(cls.CURRENT_COHORT)
                        and "实习" not in text,
                    }
                )
        return entries

    def _select_project_scope(self, parsed: dict[str, Any]) -> dict[str, Any] | None:
        self.requested_project_ids = list(parsed["project_ids"])
        payload = self._get_json(self.PROJECT_MAPPING_API)
        entries = self._mapping_entries(payload)

        if not entries:
            # Continue in an explicitly unconfirmed mode if the auxiliary
            # mapping endpoint is temporarily unavailable. The list API is
            # still read-only, while the evidence prevents downstream code
            # from treating the cohort as verified.
            self.project_mapping_request_failed = True
            selected = self.requested_project_ids or list(self.DEFAULT_FORMAL_MAPPING_IDS)
            self.selected_project_ids = selected
            self.cohort_status = "unconfirmed"
            self.campaign_validated = False
            self.cohort_evidence = "官方项目映射接口不可用；使用请求中的项目或默认项目 ID"
            self.campaign_evidence = self.cohort_evidence
        else:
            current = {
                entry["mapping_id"]: entry
                for entry in entries
                if entry["is_current_formal"]
            }
            requested = self.requested_project_ids
            selected = (
                [project_id for project_id in requested if project_id in current]
                if requested
                else list(current)
            )
            self.excluded_project_ids = [
                project_id for project_id in requested if project_id not in current
            ]
            if not selected:
                self.pagination_termination_reason = "no_current_formal_project"
                self.campaign_evidence = "请求项目不属于已确认的当前校园招聘项目"
                self.cohort_evidence = self.campaign_evidence
                return None

            self.selected_project_ids = selected
            names = [current[project_id]["project_name"] for project_id in selected]
            names = [name for name in names if name]
            self.cohort_status = "confirmed"
            self.campaign_validated = not self.excluded_project_ids
            self.cohort_evidence = "；".join(
                f"{name}（{self.CURRENT_COHORT}）" for name in names
            ) or f"官方项目映射确认 {self.CURRENT_COHORT}"
            self.campaign_evidence = (
                f"{self.PROJECT_MAPPING_API}: {self.cohort_evidence}"
            )

        if self.selected_project_ids:
            self.campaign_text = (
                f"腾讯{self.CURRENT_COHORT}校园招聘"
                + (f"（项目 {','.join(map(str, self.selected_project_ids))}）")
            )
        self.requested_filters = {
            "project_mapping_ids": list(self.selected_project_ids),
            "bg_ids": list(parsed["bg_ids"]),
            "work_city_ids": list(parsed["work_city_ids"]),
            "recruit_city_ids": list(parsed["recruit_city_ids"]),
            "position_family_ids": list(parsed["position_family_ids"]),
            "keyword": parsed["keyword"],
            "work_country_type": parsed["work_country_type"],
        }
        return self.requested_filters

    def _search_body(self, page: int, filters: dict[str, Any]) -> dict[str, Any]:
        """Build the same filter contract used by the official post page."""

        return {
            "projectIdList": [],
            "projectMappingIdList": list(filters["project_mapping_ids"]),
            "keyword": filters["keyword"],
            "bgList": list(filters["bg_ids"]),
            "workCountryType": filters["work_country_type"],
            "workCityList": list(filters["work_city_ids"]),
            "recruitCityList": list(filters["recruit_city_ids"]),
            "positionFidList": list(filters["position_family_ids"]),
            "pageIndex": page,
            "pageSize": self.PAGE_SIZE,
        }

    def _list_page(self, page: int, filters: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
        body = self._search_body(page, filters)
        payload = self._post_json(self.LIST_API, body)
        if not self._is_success(payload):
            raise RuntimeError(f"list request failed on page {page}")

        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise RuntimeError(f"list response missing data on page {page}")
        rows = data.get("positionList")
        if rows is None:
            rows = data.get("list")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError(f"list response has invalid rows on page {page}")
        raw_total = data.get("count")
        if raw_total is None:
            raw_total = data.get("total")
        total = self._as_int(raw_total)
        return [dict(row) for row in rows if isinstance(row, Mapping)], total

    @classmethod
    def _row_post_id(cls, row: Mapping[str, Any]) -> str:
        for key in ("postId", "post_id", "sourceJobId", "source_id", "id"):
            value = row.get(key)
            text = cls._compact(value)
            if text:
                return text
        return ""

    @classmethod
    def _row_is_internship(cls, row: Mapping[str, Any]) -> bool:
        text = " ".join(
            cls._compact(row.get(key))
            for key in ("projectName", "recruitLabelName", "recruitTypeName", "jobType")
        )
        return "实习" in text

    def _fetch_listing_rows(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        expected_total: int | None = None
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        for page in range(1, self.MAX_PAGES + 1):
            try:
                page_rows, page_total = self._list_page(page, filters)
            except (RuntimeError, requests.RequestException, ValueError) as exc:
                self.fetch_failed = True
                self.pagination_termination_reason = f"list_request_failed_page_{page}"
                logger.warning("Tencent list page %s failed: %s", page, exc)
                break

            self.pages_seen = page
            self.pages_fetched = page
            self.page_count = page
            if expected_total is None and page_total is not None:
                expected_total = page_total
                self.expected_total = page_total
                self.advertised_total = page_total
                self.total_count = page_total
                self.total_pages = max(1, math.ceil(page_total / self.PAGE_SIZE))
                self.expected_pages = self.total_pages
            if self.total_pages is not None and page > self.total_pages:
                self.total_pages = page
                self.expected_pages = page
            elif (
                expected_total is not None
                and page_total is not None
                and page_total != expected_total
            ):
                self.pagination_termination_reason = "advertised_total_changed"
                break

            self.listed_count += len(page_rows)
            self.raw_listed_count = self.listed_count
            for row in page_rows:
                post_id = self._row_post_id(row)
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                rows.append(row)
            self.unique_listed_count = len(seen_ids)

            if not page_rows:
                if expected_total is None or self.unique_listed_count == expected_total:
                    self.pagination_complete = True
                    self.pagination_termination_reason = (
                        "empty_result" if self.unique_listed_count == 0 else "empty_page_after_total"
                    )
                else:
                    self.pagination_termination_reason = "empty_page_before_total"
                break

            if expected_total is not None:
                if self.unique_listed_count == expected_total:
                    self.pagination_complete = True
                    self.pagination_termination_reason = "api_total_reached"
                    break
                if self.unique_listed_count > expected_total:
                    self.pagination_termination_reason = "api_count_mismatch"
                    break
                # Do not use a short-page heuristic while the API advertises a
                # total: the service may cap pageSize below our requested size.
                continue

            if len(page_rows) < self.PAGE_SIZE:
                self.pagination_complete = True
                self.pagination_termination_reason = "short_page"
                self.total_pages = self.pages_seen
                self.expected_pages = self.pages_seen
                break
        else:
            self.pagination_termination_reason = "max_pages_reached"

        self.has_more = not self.pagination_complete
        self.unique_listed_count = len(seen_ids)
        self.filtered_internship_count = sum(
            1 for row in rows if self._row_is_internship(row)
        )
        # Preserve every advertised row so pagination evidence closes against
        # the official total. The shared acceptance layer owns batch filtering.
        return rows

    @classmethod
    def _detail_texts(cls, detail: Mapping[str, Any]) -> tuple[str, str]:
        duty = cls._plain_text(
            detail.get("desc")
            or detail.get("topicDetail")
            or detail.get("introduction")
            or detail.get("jobDescription")
        )
        requirement = cls._plain_text(
            detail.get("request")
            or detail.get("topicRequirement")
            or detail.get("requirement")
            or detail.get("jobRequirement")
        )
        return duty, requirement

    def _fetch_detail(self, post_id: str) -> dict[str, Any] | None:
        payload = self._get_json(self.DETAIL_API, params={"postId": post_id})
        if not self._is_success(payload):
            return None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return None
        returned_id = self._row_post_id(data)
        if returned_id and returned_id != post_id:
            return None
        return dict(data)

    def _job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        post_id = self._row_post_id(row)
        if not post_id:
            return None

        detail = self._fetch_detail(post_id)
        if detail is None:
            self.detail_failures.append(
                {
                    "post_id": post_id,
                    "reason": "detail_request_failed_or_id_mismatch",
                }
            )
            return self._job_stub_from_row(row, post_id)

        duty, requirement = self._detail_texts(detail)
        if not duty or not requirement:
            missing = []
            if not duty:
                missing.append("duty")
            if not requirement:
                missing.append("requirement")
            self.detail_failures.append(
                {"post_id": post_id, "reason": "missing_" + "_and_".join(missing)}
            )
            return self._job_stub_from_row(row, post_id)

        title = self._compact(
            detail.get("title") or detail.get("positionTitle") or row.get("positionTitle")
        )
        if not title:
            self.detail_failures.append({"post_id": post_id, "reason": "missing_title"})
            return self._job_stub_from_row(row, post_id)
        city = self._city_text(
            detail.get("workCityList")
            or detail.get("workCity")
            or row.get("workCities")
        )
        project_name = self._compact(
            detail.get("projectName")
            or row.get("projectName")
            or detail.get("recruitLabelName")
            or row.get("recruitLabelName")
            or "校招"
        )
        jd_raw = f"岗位职责\n{duty}\n\n任职要求\n{requirement}"
        detail_url = self.DETAIL_URL_TEMPLATE.format(post_id=post_id)
        stable_id = f"tencent:{post_id}"
        job = self._make_job(
            title=title,
            city=city,
            job_type=project_name,
            jd_url=detail_url,
            jd_raw=jd_raw,
            published_at=self._compact(
                detail.get("publishTime") or detail.get("publishedAt") or row.get("publishTime")
            ),
            campaign_text=self.campaign_text,
        )
        job.update(
            {
                "id": stable_id,
                "source_id": post_id,
                "source_job_id": post_id,
                "external_id": post_id,
                "detail_url": detail_url,
                "source_post_id": post_id,
                "batch": "formal",
                "recruitment_track": "formal",
                "cohort": self.cohort,
                "cohort_status": self.cohort_status,
                "cohort_source": self.cohort_source,
                "cohort_evidence": self.cohort_evidence,
                "campaign_evidence": self.campaign_evidence,
                "source_read_only": True,
            }
        )
        return job

    def _job_stub_from_row(self, row: Mapping[str, Any], post_id: str) -> dict[str, Any]:
        """Retain an advertised row so JD QA can reject it with evidence."""

        detail_url = self.DETAIL_URL_TEMPLATE.format(post_id=post_id)
        project_name = self._compact(
            row.get("projectName") or row.get("recruitLabelName") or "校招"
        )
        job = self._make_job(
            title=self._compact(row.get("positionTitle") or row.get("title")) or f"岗位 {post_id}",
            city=self._city_text(row.get("workCities") or row.get("workCityList")),
            job_type=project_name,
            jd_url=detail_url,
            jd_raw="",
            campaign_text=self.campaign_text,
        )
        job.update({
            "id": f"tencent:{post_id}",
            "source_id": post_id,
            "source_job_id": post_id,
            "external_id": post_id,
            "detail_url": detail_url,
            "source_post_id": post_id,
            "cohort": self.cohort,
            "cohort_status": self.cohort_status,
            "cohort_source": self.cohort_source,
            "cohort_evidence": self.cohort_evidence,
            "campaign_evidence": self.campaign_evidence,
            "source_read_only": True,
        })
        return job

    def _update_metrics(self) -> None:
        self.adapter_complete = bool(
            self.pagination_complete
            and self.detail_complete
            and self.campaign_validated
            and not self.fetch_failed
        )
        self.metrics = {
            "read_only": True,
            "source_kind": self.source_kind,
            "resolved_source_url": self.resolved_source_url,
            "requested_filters": dict(self.requested_filters),
            "selected_project_ids": list(self.selected_project_ids),
            "share_id": self.share_id,
            "share_requires_login": self.share_requires_login,
            "share_scope": self.share_scope,
            "campaign_validated": self.campaign_validated,
            "campaign_evidence": self.campaign_evidence,
            "project_mapping_request_failed": self.project_mapping_request_failed,
            "fetch_failed": self.fetch_failed,
            "cohort": self.cohort,
            "cohort_status": self.cohort_status,
            "cohort_evidence": self.cohort_evidence,
            "pagination": self.pagination_metrics(),
            "detail": self.detail_metrics(),
            "complete": self.adapter_complete,
        }
        self.completeness_evidence = {
            "source_url": self.careers_url,
            "effective_source_url": self.resolved_source_url,
            "pagination_complete": self.pagination_complete,
            "pages_seen": self.pages_seen,
            "total_pages": self.total_pages,
            "advertised_total": self.advertised_total,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "has_more": self.has_more,
            "termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
            "detail_complete": self.detail_complete,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_failures": list(self.detail_failures),
            "share_id": self.share_id,
            "share_requires_login": self.share_requires_login,
            "share_scope": self.share_scope,
            "campaign_validated": self.campaign_validated,
            "cohort_status": self.cohort_status,
            "cohort_evidence": self.cohort_evidence,
            "read_only": True,
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return {
            "pages_seen": self.pages_seen,
            "pages_fetched": self.pages_fetched,
            "page_count": self.page_count,
            "total_pages": self.total_pages,
            "expected_pages": self.expected_pages,
            "advertised_total": self.advertised_total,
            "expected_total": self.expected_total,
            "total_count": self.total_count,
            "raw_listed_count": self.raw_listed_count,
            "listed_count": self.listed_count,
            "unique_listed_count": self.unique_listed_count,
            "filtered_internship_count": self.filtered_internship_count,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
        }

    def detail_metrics(self) -> dict[str, Any]:
        return {
            "expected_total": self.detail_expected_total,
            "count": self.detail_count,
            "complete": self.detail_complete,
            "failures": list(self.detail_failures),
        }

    def integrity_evidence(self) -> dict[str, Any]:
        """Return compact, serializable evidence for crawler audit consumers."""

        self._update_metrics()
        return {
            "adapter": "tencent",
            "read_only": True,
            "requested_url": self.careers_url,
            "resolved_source_url": self.resolved_source_url,
            "source_kind": self.source_kind,
            "share_id": self.share_id,
            "share_requires_login": self.share_requires_login,
            "share_scope": self.share_scope,
            "campaign_validated": self.campaign_validated,
            "campaign_evidence": self.campaign_evidence,
            "project_mapping_request_failed": self.project_mapping_request_failed,
            "fetch_failed": self.fetch_failed,
            "api_usage": {
                "project_mapping": {
                    "method": "GET",
                    "url": self.PROJECT_MAPPING_API,
                },
                "position_list": {
                    "method": "POST",
                    "url": self.LIST_API,
                    "pagination": "pageIndex/pageSize",
                },
                "job_detail": {
                    "method": "GET",
                    "url": self.DETAIL_API,
                    "params": "postId",
                },
                "share_recommendation": "not_requested_without_login",
            },
            "cohort": self.cohort,
            "cohort_status": self.cohort_status,
            "cohort_source": self.cohort_source,
            "cohort_evidence": self.cohort_evidence,
            "pagination": self.pagination_metrics(),
            "detail": self.detail_metrics(),
            "complete": self.adapter_complete,
        }

    def fetch(self) -> list[dict[str, Any]]:
        self._reset_state()
        parsed = self.parse_source_url(self.careers_url)
        self.source_kind = parsed["source_kind"]
        self.share_id = parsed["share_id"]
        self.share_requires_login = parsed["share_requires_login"]
        if self.share_requires_login:
            self.share_scope = "login_required"
            self.resolved_source_url = parsed["share_url"] or parsed["normalized_url"]
        else:
            self.share_scope = ""
            self.resolved_source_url = parsed["normalized_url"]

        if parsed["source_kind"] == "unsupported" or parsed["host"] not in {
            "join.qq.com",
            "www.join.qq.com",
        }:
            self.fetch_failed = True
            self.pagination_termination_reason = "unsupported_source_url"
            self._update_metrics()
            return []

        if self.share_requires_login:
            self.pagination_termination_reason = "share_requires_login"
            self.campaign_evidence = (
                "腾讯伯乐分享链接需要登录；未提交凭证，未读取私有推荐数据"
            )
            self.cohort_evidence = self.campaign_evidence
            self.detail_complete = True
            self._update_metrics()
            return []

        filters = self._select_project_scope(parsed)
        if filters is None:
            self.detail_complete = True
            self._update_metrics()
            return []

        rows = self._fetch_listing_rows(filters)
        self.detail_expected_total = len(rows)
        indexed_jobs: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(self.DETAIL_WORKERS, max(1, len(rows)))) as pool:
            futures = {
                pool.submit(self._job_from_row, row): index
                for index, row in enumerate(rows)
            }
            for future in as_completed(futures):
                job = future.result()
                if job is not None:
                    indexed_jobs[futures[future]] = job
                    if str(job.get("jd_raw") or "").strip():
                        self.detail_count += 1
        jobs = [indexed_jobs[index] for index in sorted(indexed_jobs)]
        self.detail_complete = (
            not self.detail_failures and self.detail_count == self.detail_expected_total
        )
        self._update_metrics()
        return jobs
