"""理想汽车官方 2027 校园招聘 API 爬虫。

理想汽车的职位卡没有岗位级网页，前端通过官方 API 加载列表和详情弹窗。
本爬虫固定使用 ``project_id=18``，避免把「理想+」、实习生招聘或往届项目
混入 2027 正式岗位。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class LixiangCrawler(BaseCrawler):
    """抓取理想汽车 ``2027校园招聘`` 的完整岗位和 JD。"""

    PROJECT_API = (
        "https://api-web.lixiang.com/osd-hr-recruitment-website/v1/"
        "recruit/school/project/list"
    )
    LIST_API = (
        "https://api-web.lixiang.com/osd-hr-recruitment-website/v1/"
        "recruit/school/job-page"
    )
    DETAIL_API = (
        "https://api-web.lixiang.com/osd-hr-recruitment-website/v1/"
        "recruit/job/detail"
    )
    CANONICAL_LIST_URL = (
        "https://www.lixiang.com/employ/campus/list.html"
        "?fromJob=1&project_id=18"
    )

    TARGET_PROJECT_ID = 18
    TARGET_PROJECT_NAME = "2027校园招聘"
    PAGE_SIZE = 100
    MAX_PAGES = 20
    REQUEST_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 0.4
    JD_RAW_LIMIT = 12000
    _INTERNSHIP_TITLE_RE = re.compile(r"实习|intern", re.IGNORECASE)
    _INTERNSHIP_JD_RE = re.compile(
        r"(?:职位|岗位|工作|招聘)(?:性质|类型|类别)?\s*[：:]\s*(?:实习|intern)"
        r"|(?:实习生招聘|实习岗位|实习职位|招聘实习生)",
        re.IGNORECASE,
    )

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.session = requests.Session()
        self._reset_state()

    def _reset_state(self) -> None:
        self.expected_total: int | None = None
        self.expected_pages: int | None = None
        self.pages_fetched = 0
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.detail_expected_total = 0
        # detail_count counts complete details, including rows later removed
        # because their title/JD is an internship.
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []

        self.project_validated = False
        self.project_name = ""
        self.excluded_records: list[dict[str, str]] = []
        self.filtered_internship_count = 0
        self.metrics: dict[str, Any] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        """Expose both flat aliases and grouped metrics for audit callers."""
        pagination = {
            "expected_total": self.expected_total,
            "expected_pages": self.expected_pages,
            "pages_fetched": self.pages_fetched,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
        }
        detail = {
            "expected_total": self.detail_expected_total,
            "count": self.detail_count,
            "complete": self.detail_complete,
            "failures": list(self.detail_failures),
        }
        self.metrics = {
            "expected_total": self.expected_total,
            "expected_pages": self.expected_pages,
            "pages": self.pages_fetched,
            "pages_fetched": self.pages_fetched,
            "pagination_complete": self.pagination_complete,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_complete": self.detail_complete,
            "filtered_internship_count": self.filtered_internship_count,
            "excluded_count": len(self.excluded_records),
            "project_validated": self.project_validated,
            "project_name": self.project_name,
            "pagination": pagination,
            "detail": detail,
        }

    @classmethod
    def _is_target_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc.casefold() not in {"www.lixiang.com", "lixiang.com"}:
            return False
        if parsed.path.rstrip("/") != "/employ/campus/list.html":
            return False
        values = parse_qs(parsed.query).get("project_id") or []
        return values == [str(cls.TARGET_PROJECT_ID)]

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.CANONICAL_LIST_URL,
            "Origin": "https://www.lixiang.com",
        }

    def _get_json(
        self,
        url: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("payload_not_object")
                return payload
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] 理想汽车 API 请求失败 %s: %s",
                        self.company_name,
                        url,
                        exc,
                    )
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
        return None

    @staticmethod
    def _api_data(payload: dict[str, object] | None) -> dict[str, object] | None:
        if not isinstance(payload, dict):
            return None
        code = payload.get("code")
        if code not in (None, 0, "0"):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _validate_project(self) -> bool:
        data = self._api_data(self._get_json(self.PROJECT_API))
        if data is None:
            return False

        projects = data.get("item") or data.get("items")
        if not isinstance(projects, list):
            return False
        for project in projects:
            if not isinstance(project, dict):
                continue
            try:
                project_id = int(project.get("id"))
            except (TypeError, ValueError):
                continue
            if project_id != self.TARGET_PROJECT_ID:
                continue
            self.project_name = str(project.get("name") or "").strip()
            self.project_validated = self.project_name == self.TARGET_PROJECT_NAME
            return self.project_validated
        return False

    @staticmethod
    def _as_int(value: object) -> int | None:
        try:
            return None if value in (None, "") else int(value)
        except (TypeError, ValueError):
            return None

    def _list_page(self, page: int) -> tuple[list[dict[str, object]], dict[str, object]]:
        payload = self._get_json(
            self.LIST_API,
            params={
                "page": page,
                "page_size": self.PAGE_SIZE,
                "project_id": self.TARGET_PROJECT_ID,
            },
        )
        data = self._api_data(payload)
        if data is None or not isinstance(data.get("items"), list):
            raise ValueError("list_payload_missing_items")
        return [item for item in data["items"] if isinstance(item, dict)], data

    def _fetch_listing_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            try:
                page_items, meta = self._list_page(page)
            except (ValueError, TypeError, requests.RequestException) as exc:
                self.pagination_termination_reason = f"list_failed_page_{page}"
                logger.warning(
                    "[%s] 理想汽车第 %d 页失败: %s",
                    self.company_name,
                    page,
                    exc,
                )
                break

            self.pages_fetched = page
            page_total = self._as_int(meta.get("total_count"))
            page_count = self._as_int(meta.get("total_pages"))
            if self.expected_total is None:
                self.expected_total = page_total
            elif page_total is not None and page_total != self.expected_total:
                self.pagination_termination_reason = "total_changed_between_pages"
                break
            if self.expected_pages is None:
                self.expected_pages = page_count
            elif page_count is not None and page_count != self.expected_pages:
                self.pagination_termination_reason = "page_count_changed_between_pages"
                break

            if not page_items:
                self.pagination_termination_reason = "empty_page"
                break

            self.raw_listed_count += len(page_items)
            for row in page_items:
                source_id = str(row.get("id") or "").strip()
                if not source_id:
                    self.pagination_termination_reason = "row_id_missing"
                    break
                if source_id in seen_ids:
                    continue
                seen_ids.add(source_id)
                rows.append(row)
            else:
                self.unique_listed_count = len(seen_ids)
                if self.expected_pages is not None and page >= self.expected_pages:
                    self.pagination_termination_reason = "expected_pages_reached"
                    self.pagination_complete = (
                        self.expected_total is not None
                        and self.unique_listed_count == self.expected_total
                    )
                    break
                if self.expected_pages is None and (
                    len(page_items) < self.PAGE_SIZE
                    and self.expected_total is not None
                ):
                    self.pagination_termination_reason = "short_page"
                    self.pagination_complete = (
                        self.unique_listed_count == self.expected_total
                    )
                    break
                continue
            break
        else:
            self.pagination_termination_reason = "max_pages"

        self.unique_listed_count = len(seen_ids)
        if not self.pagination_complete and self.pagination_termination_reason == "not_started":
            self.pagination_termination_reason = "pagination_incomplete"
        self._update_metrics()
        return rows

    @staticmethod
    def _plain_text(value: object) -> str:
        if value is None:
            return ""
        soup = BeautifulSoup(str(value), "html.parser")
        lines = []
        for line in soup.get_text("\n", strip=True).replace("\r", "").splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    @classmethod
    def _jd_text(cls, detail: dict[str, object]) -> tuple[str, str, str]:
        description = cls._plain_text(detail.get("description"))
        requirements = cls._plain_text(detail.get("requirements"))
        parts = []
        if description:
            parts.extend(["职位描述", description])
        if requirements:
            parts.extend(["任职要求", requirements])
        return description, requirements, "\n".join(parts)[: cls.JD_RAW_LIMIT]

    def _detail_record(self, source_id: str) -> dict[str, object] | None:
        payload = self._get_json(self.DETAIL_API, params={"job_id": source_id})
        data = self._api_data(payload)
        if data is None or str(data.get("id") or "") != source_id:
            return None
        return data

    @classmethod
    def _is_internship(cls, title: str, jd_raw: str) -> bool:
        if cls._INTERNSHIP_TITLE_RE.search(title):
            return True
        normalized = re.sub(r"\s+", " ", jd_raw or "").strip()
        if cls._INTERNSHIP_JD_RE.search(normalized):
            return True
        return any(
            line.strip().casefold() in {"实习", "实习生", "intern", "internship"}
            for line in (jd_raw or "").splitlines()
        )

    def _make_job(
        self,
        row: dict[str, object],
        detail: dict[str, object],
        jd_raw: str,
    ) -> dict[str, object]:
        source_id = str(row.get("id") or "").strip()
        job = super()._make_job(
            title=str(detail.get("title") or row.get("title") or "").strip(),
            city=str(detail.get("location_title") or row.get("location_title") or "").strip(),
            job_type="校招",
            jd_url=self.CANONICAL_LIST_URL,
            jd_raw=jd_raw,
            link_kind="list",
            campaign_text=self.TARGET_PROJECT_NAME,
        )
        job.update(
            {
                "source_job_id": source_id,
                "cohort": 2027,
                "cohort_status": "confirmed",
                "cohort_source": "理想汽车官方项目接口",
                "cohort_evidence": self.TARGET_PROJECT_NAME,
                "recruitment_track": "formal",
            }
        )
        return job

    def _hydrate_details(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        self.detail_expected_total = len(rows)
        jobs: list[dict[str, object]] = []
        for row in rows:
            source_id = str(row.get("id") or "").strip()
            detail = self._detail_record(source_id)
            list_url = self.CANONICAL_LIST_URL
            if detail is None:
                self.detail_failures.append(
                    {"id": source_id, "url": list_url, "reason": "detail_missing"}
                )
                continue

            description, requirements, jd_raw = self._jd_text(detail)
            if not description or not requirements:
                self.detail_failures.append(
                    {
                        "id": source_id,
                        "url": list_url,
                        "reason": "missing_jd:description_or_requirements",
                    }
                )
                continue
            self.detail_count += 1

            if str(detail.get("subject_name") or "").strip() != self.TARGET_PROJECT_NAME:
                self.detail_failures.append(
                    {"id": source_id, "url": list_url, "reason": "wrong_project"}
                )
                continue
            if str(detail.get("job_mode_name") or "").strip() != "正式":
                self.excluded_records.append(
                    {"id": source_id, "title": str(detail.get("title") or row.get("title") or ""), "reason": "non_formal"}
                )
                continue
            if detail.get("is_prior") not in (None, 0, "0", False):
                self.excluded_records.append(
                    {"id": source_id, "title": str(detail.get("title") or row.get("title") or ""), "reason": "early_batch"}
                )
                continue

            title = str(detail.get("title") or row.get("title") or "").strip()
            if self._is_internship(title, jd_raw):
                self.filtered_internship_count += 1
                self.excluded_records.append(
                    {"id": source_id, "title": title, "reason": "internship_title_or_jd"}
                )
                continue
            jobs.append(self._make_job(row, detail, jd_raw))

        self.listed_count = len(jobs)
        self.detail_complete = (
            self.detail_count == self.detail_expected_total
            and not self.detail_failures
        )
        self._update_metrics()
        return jobs

    def fetch(self) -> list[dict]:
        self._reset_state()
        if not self._is_target_url(self.careers_url):
            self.pagination_termination_reason = "invalid_project_scope"
            self._update_metrics()
            return []
        if not self._validate_project():
            self.pagination_termination_reason = "project_validation_failed"
            self._update_metrics()
            return []

        rows = self._fetch_listing_rows()
        if not self.pagination_complete:
            self._update_metrics()
            return []
        jobs = self._hydrate_details(rows)
        self._update_metrics()
        return jobs if self.detail_complete else []


__all__ = ["LixiangCrawler"]
