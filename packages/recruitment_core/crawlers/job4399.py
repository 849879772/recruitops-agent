"""Deterministic adapter for the 4399 campus recruitment platform.

The page has six category tabs. Every category has one server-rendered page,
then loads further rows from ``job/agentMore`` while scrolling. Company-copy
"查看更多" links are deliberately ignored because they are not job pagination.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Category:
    type_id: str
    label: str


class Job4399Crawler(BaseCrawler):
    """Read every 4399 job category and prove termination with an empty API page."""

    MAX_PAGES_PER_CATEGORY = 50

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_evidence()

    @staticmethod
    def supports(url: str) -> bool:
        return (urlsplit(str(url or "")).hostname or "").casefold() == "hr.4399om.com"

    def _reset_evidence(self) -> None:
        self.resolved_source_url = self.careers_url
        self.fetch_failed = False
        self.pagination_complete = False
        self.pages_seen = 0
        self.pages_fetched = 0
        self.total_pages = None
        self.expected_pages = None
        self.advertised_total = None
        self.expected_total = None
        self.has_more = True
        self.pagination_termination_reason = "not_started"
        self.pagination_evidence: list[dict[str, Any]] = []
        self.completeness_evidence: dict[str, Any] = {}
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.duplicate_count = 0
        self.categories_seen = 0
        self.detail_complete = False
        self.jd_status = "list_only_not_hydrated"

    @staticmethod
    def _text(node) -> str:
        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

    @staticmethod
    def _response_text(response: object) -> str:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return str(getattr(response, "text", "") or "")

    @classmethod
    def _response_json(cls, response: object) -> object:
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            return json.loads(content.decode("utf-8"))
        return response.json()

    @classmethod
    def _categories(cls, html: str) -> list[_Category]:
        soup = BeautifulSoup(html or "", "html.parser")
        categories: list[_Category] = []
        seen: set[str] = set()
        for node in soup.select(".searchItems_item[data-type]"):
            type_id = str(node.get("data-type") or "").strip()
            label = cls._text(node)
            if not type_id or not label or type_id in seen:
                continue
            seen.add(type_id)
            categories.append(_Category(type_id=type_id, label=label))
        return categories

    def _route_url(self, route: str, **updates: object) -> str:
        parsed = urlsplit(self.careers_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["r"] = route
        for key, value in updates.items():
            query[key] = str(value)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/weixin/",
                urlencode(query, safe="/"),
                "",
            )
        )

    @staticmethod
    def _native_id(url: str) -> str:
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        return str(query.get("id") or query.get("jobid") or "").strip()

    def _make_record(
        self,
        *,
        source_job_id: str,
        title: str,
        city: str,
        category: str,
        source_list_url: str,
        detail_url: str,
    ) -> dict[str, Any] | None:
        title = " ".join(str(title or "").split())
        if not title:
            return None
        job = self._make_job(
            title=title,
            city=" ".join(str(city or "").split()),
            job_type="校招",
            jd_url=detail_url,
            jd_raw="",
            link_kind="detail",
            campaign_text="2027校园招聘",
        )
        job.update(
            source_list_url=source_list_url,
            source_job_id=source_job_id,
            category=" ".join(str(category or "").split()),
            jd_raw_complete=False,
            detail_complete=False,
            jd_status=self.jd_status,
        )
        return job

    def _parse_jobs(
        self,
        html: str,
        *,
        source_url: str | None = None,
        category_label: str = "",
    ) -> list[dict[str, Any]]:
        source_url = source_url or self.careers_url
        soup = BeautifulSoup(html or "", "html.parser")
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in soup.select(".postList .postItem, .postItem"):
            anchor = row.select_one('a[href*="job/view"]')
            if anchor is None:
                continue
            detail_url = urljoin(source_url, str(anchor.get("href") or ""))
            source_job_id = self._native_id(detail_url)
            title = self._text(row.select_one(".postItem_name"))
            category = self._text(row.select_one(".postItem_category")) or category_label
            city = self._text(row.select_one(".postItem_location span"))
            identity = source_job_id.casefold() or detail_url.casefold()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            record = self._make_record(
                source_job_id=source_job_id,
                title=title,
                city=city,
                category=category,
                source_list_url=source_url,
                detail_url=detail_url,
            )
            if record is not None:
                jobs.append(record)
        return jobs

    def _payload_jobs(
        self,
        payload: object,
        *,
        category: _Category,
        source_list_url: str,
    ) -> list[dict[str, Any]] | None:
        if isinstance(payload, dict):
            rows = list(payload.items())
        elif isinstance(payload, list):
            rows = [
                (str(row.get("id") or row.get("jobId") or index), row)
                for index, row in enumerate(payload, start=1)
                if isinstance(row, dict)
            ]
        else:
            return None

        jobs: list[dict[str, Any]] = []
        for source_job_id, row in rows:
            if not isinstance(row, dict):
                continue
            detail_url = self._route_url(
                "job/view", id=source_job_id, type="agent", jobTableType=1
            )
            record = self._make_record(
                source_job_id=str(source_job_id),
                title=str(row.get("name") or row.get("title") or ""),
                city=str(row.get("workCity") or row.get("city") or ""),
                category=str(row.get("type") or category.label),
                source_list_url=source_list_url,
                detail_url=detail_url,
            )
            if record is not None:
                jobs.append(record)
        return jobs

    @staticmethod
    def _identity(job: dict[str, Any]) -> str:
        source_job_id = str(job.get("source_job_id") or "").strip().casefold()
        if source_job_id:
            return f"id:{source_job_id}"
        return f"url:{str(job.get('jd_url') or '').strip().casefold()}"

    def _append_unique(
        self,
        target: list[dict[str, Any]],
        seen: set[str],
        rows: list[dict[str, Any]],
    ) -> None:
        self.raw_listed_count += len(rows)
        for row in rows:
            identity = self._identity(row)
            if identity in seen:
                self.duplicate_count += 1
                continue
            seen.add(identity)
            target.append(row)

    def _set_final_evidence(
        self,
        jobs: list[dict[str, Any]],
        *,
        expected_categories: int,
        completed_categories: int,
    ) -> None:
        self.unique_listed_count = len(jobs)
        self.categories_seen = expected_categories
        self.completeness_evidence = {
            "source_url": self.careers_url,
            "effective_source_url": self.resolved_source_url,
            "categories_expected": expected_categories,
            "categories_completed": completed_categories,
            "pages_seen": self.pages_seen,
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "duplicate_count": self.duplicate_count,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "termination_reason": self.pagination_termination_reason,
            "fetch_failed": self.fetch_failed,
            "jd_status": self.jd_status,
            "detail_complete": self.detail_complete,
            "read_only": True,
            "evidence": list(self.pagination_evidence),
        }

    def pagination_metrics(self) -> dict[str, Any]:
        return {
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "pages_seen": self.pages_seen,
            "total_pages": self.total_pages,
            "has_more": self.has_more,
            "evidence": list(self.pagination_evidence),
        }

    def fetch(self) -> list[dict[str, Any]]:
        self._reset_evidence()
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        completed_categories = 0

        first = self._get(self.careers_url, timeout=25)
        if first is None:
            self.fetch_failed = True
            self.pagination_termination_reason = "initial_page_fetch_failed"
            self._set_final_evidence(jobs, expected_categories=0, completed_categories=0)
            return jobs

        self.pages_seen = self.pages_fetched = 1
        self.resolved_source_url = str(getattr(first, "url", "") or self.careers_url)
        first_html = self._response_text(first)
        categories = self._categories(first_html)
        if not categories:
            self.pagination_termination_reason = "category_controls_missing"
            self._set_final_evidence(jobs, expected_categories=0, completed_categories=0)
            return jobs

        current_type = dict(parse_qsl(urlsplit(self.resolved_source_url).query)).get("type")
        for category in categories:
            category_url = self._route_url(
                "job/agent", type=category.type_id, isOpen=0, jobTableType=1
            )
            if category.type_id == current_type:
                category_html = first_html
            else:
                response = self._get(category_url, timeout=25)
                if response is None:
                    self.fetch_failed = True
                    self.pagination_termination_reason = "category_page_fetch_failed"
                    self.pagination_evidence.append(
                        {"category": category.label, "type": category.type_id, "status": "fetch_failed"}
                    )
                    break
                self.pages_seen += 1
                self.pages_fetched += 1
                category_html = self._response_text(response)

            initial_rows = self._parse_jobs(
                category_html,
                source_url=category_url,
                category_label=category.label,
            )
            self._append_unique(jobs, seen, initial_rows)
            page_number = 2
            page_signatures: set[tuple[str, ...]] = set()
            category_complete = False

            while page_number <= self.MAX_PAGES_PER_CATEGORY:
                more_url = self._route_url(
                    "job/agentMore",
                    p=page_number,
                    type=category.type_id,
                    key="",
                    jobTableType=1,
                )
                response = self._get(more_url, timeout=25)
                if response is None:
                    self.fetch_failed = True
                    self.pagination_termination_reason = "category_more_fetch_failed"
                    break
                self.pages_seen += 1
                self.pages_fetched += 1
                try:
                    payload = self._response_json(response)
                except (AttributeError, TypeError, UnicodeDecodeError, ValueError):
                    payload = None
                rows = self._payload_jobs(
                    payload,
                    category=category,
                    source_list_url=category_url,
                )
                if rows is None:
                    self.fetch_failed = True
                    self.pagination_termination_reason = "category_more_invalid_payload"
                    break
                if not rows:
                    category_complete = True
                    completed_categories += 1
                    self.pagination_evidence.append(
                        {
                            "category": category.label,
                            "type": category.type_id,
                            "page": page_number,
                            "rows": 0,
                            "terminal": True,
                        }
                    )
                    break

                signature = tuple(sorted(self._identity(row) for row in rows))
                if signature in page_signatures:
                    self.pagination_termination_reason = "category_page_repeated"
                    break
                page_signatures.add(signature)
                self._append_unique(jobs, seen, rows)
                self.pagination_evidence.append(
                    {
                        "category": category.label,
                        "type": category.type_id,
                        "page": page_number,
                        "rows": len(rows),
                        "terminal": False,
                    }
                )
                page_number += 1

            if not category_complete:
                if page_number > self.MAX_PAGES_PER_CATEGORY:
                    self.pagination_termination_reason = "category_page_safety_limit"
                break

        self.pagination_complete = (
            bool(categories)
            and completed_categories == len(categories)
            and not self.fetch_failed
        )
        self.has_more = not self.pagination_complete
        if self.pagination_complete:
            self.pagination_termination_reason = "all_categories_explicit_empty_terminal"
            self.total_pages = self.pages_seen
            self.expected_pages = self.pages_seen
        elif self.pagination_termination_reason == "not_started":
            self.pagination_termination_reason = "pagination_incomplete"

        self._set_final_evidence(
            jobs,
            expected_categories=len(categories),
            completed_categories=completed_categories,
        )
        logger.info(
            "[%s] 4399 抓到 %d 个岗位，分类=%d/%d，分页=%s，原因=%s",
            self.company_name,
            len(jobs),
            completed_categories,
            len(categories),
            self.pagination_complete,
            self.pagination_termination_reason,
        )
        return jobs


Crawler4399 = Job4399Crawler
