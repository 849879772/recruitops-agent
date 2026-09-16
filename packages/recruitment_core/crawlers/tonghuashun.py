"""Tonghuashun campus jobs from the site's public JSON API."""

from __future__ import annotations

import logging
import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class TonghuashunCampusCrawler(BaseCrawler):
    HOST = "campus.10jqka.com.cn"
    SERIES_API = f"https://{HOST}/api/v3/recruitmentSeries/list"
    LIST_API = f"https://{HOST}/api/v3/school_recruitment/apply/apply_list"
    DETAIL_API = f"https://{HOST}/api/v3/school_recruitment/apply/apply_detail"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 12000
    MIN_INLINE_DETAIL_LENGTH = 240

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.resolved_source_url = careers_url
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False

    @classmethod
    def supports(cls, url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/").casefold()
        has_series = bool(parse_qs(parsed.query).get("sid"))
        return (
            parsed.netloc.casefold() == cls.HOST
            and (path == "/mobile/job/list" or (path == "/job/list" and has_series))
        )

    @staticmethod
    def _normalized_series_name(value: object) -> str:
        text = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").casefold())
        return text.replace("aimie", "aime")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.careers_url,
            "User-Agent": "Mozilla/5.0",
        }

    def _get_json(self, url: str, *, params: dict[str, object]) -> dict | None:
        try:
            response = requests.get(url, params=params, headers=self._headers(), timeout=25)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success") or str(payload.get("erro_code")) != "0":
                return None
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Tonghuashun API request failed: %s", self.company_name, exc)
            return None

    def _requested_series_id(self) -> str | None:
        requested = (parse_qs(urlsplit(self.careers_url).query).get("sid") or [""])[0]
        # No sid is an explicit all-series listing. Do not use company-name
        # heuristics here: the mobile page intentionally mixes campus,
        # special-program, and internship rows for downstream filtering.
        if not requested:
            return ""
        payload = self._get_json(self.SERIES_API, params={"type": 0})
        if payload is None:
            self.fetch_failed = True
            self.pagination_termination_reason = "series_request_failed"
            return None

        series = payload.get("ex_data") or []
        for item in series:
            if str(item.get("id") or "") == requested:
                return requested

        company_hint = self._normalized_series_name(self.company_name)
        for item in series:
            name = self._normalized_series_name(item.get("series_name"))
            if name and (name in company_hint or company_hint.endswith(name + "计划")):
                return str(item.get("id") or "") or None

        self.pagination_termination_reason = "recruitment_series_not_found"
        return None

    @classmethod
    def _detail_is_sufficient(cls, intro: str, requirement: str) -> bool:
        return bool(
            intro
            and requirement
            and len(intro) + len(requirement) >= cls.MIN_INLINE_DETAIL_LENGTH
        )

    def _fetch_detail_fields(self, job_id: str) -> dict | None:
        payload = self._get_json(self.DETAIL_API, params={"id": job_id})
        if not payload:
            return None
        data = payload.get("ex_data") or payload.get("data") or {}
        if isinstance(data, list):
            data = next(
                (
                    item for item in data
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == str(job_id)
                ),
                {},
            )
        return data if isinstance(data, dict) else None

    def _detail_backfill(self, item: dict, job_id: str, title: str) -> tuple[str, str]:
        intro = str(item.get("intro") or "").strip()
        requirement = str(item.get("requirement") or "").strip()
        if self._detail_is_sufficient(intro, requirement):
            return intro, requirement

        detail = self._fetch_detail_fields(job_id)
        if not detail:
            return intro, requirement
        detail_id = str(detail.get("id") or detail.get("apply_id") or "").strip()
        if detail_id and detail_id != str(job_id):
            logger.warning(
                "[%s] Tonghuashun detail identity mismatch: requested=%s observed=%s",
                self.company_name,
                job_id,
                detail_id,
            )
            return intro, requirement
        detail_title = str(detail.get("name") or detail.get("title") or "").strip()
        if detail_title and detail_title != title:
            logger.warning(
                "[%s] Tonghuashun detail title mismatch: requested=%s observed=%s",
                self.company_name,
                title,
                detail_title,
            )
            return intro, requirement
        return (
            str(detail.get("intro") or intro).strip(),
            str(detail.get("requirement") or requirement).strip(),
        )

    def _detail_url(self, job_id: str) -> str:
        parsed = urlsplit(self.careers_url)
        path = "/mobile/job/detail" if parsed.path.rstrip("/").casefold() == "/mobile/job/list" else "/job/detail"
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            path,
            urlencode({"id": job_id}),
            "",
        ))

    def _parse_job(self, item: dict) -> dict | None:
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("name") or "").strip()
        if not job_id or not title:
            return None
        intro, requirement = self._detail_backfill(item, job_id, title)
        parts = []
        if intro:
            parts.extend(["岗位职责", intro])
        if requirement:
            parts.extend(["任职要求", requirement])
        job = self._make_job(
            title=title,
            city=str(item.get("base") or "").replace(",", "、")[:80],
            jd_url=self._detail_url(job_id),
            jd_raw="\n".join(parts)[: self.JD_RAW_LIMIT],
            link_kind="detail",
        )
        job.update({
            "source_job_id": job_id,
            "source_list_url": self.careers_url,
            "recruitment_series": str(
                item.get("apply_recruitment_series_name")
                or item.get("recruitment_series")
                or ""
            ),
        })
        return job

    def fetch(self) -> list[dict]:
        series_id = self._requested_series_id()
        if series_id is None:
            return []

        jobs: list[dict] = []
        seen: set[str] = set()
        expected_total: int | None = None
        expected_pages: int | None = None
        for page in range(1, self.MAX_PAGES + 1):
            payload = self._get_json(
                self.LIST_API,
                params={
                    "applyName": "",
                    "bases": "",
                    "page": page,
                    "pageCount": self.PAGE_SIZE,
                    "applyColonyId": "",
                    "applyRecruitmentSeriesIds": series_id,
                    "type": "school",
                },
            )
            if payload is None:
                self.fetch_failed = True
                self.has_more = page > 1
                self.pagination_termination_reason = f"list_request_failed_page_{page}"
                break

            data = payload.get("ex_data") or {}
            rows = data.get("apply_show_do_list") or []
            total = int(data.get("total") or 0)
            size = int(data.get("size") or self.PAGE_SIZE)
            pages = int(data.get("pages") or (math.ceil(total / size) if total else 0))
            self.pages_seen = page
            if expected_total is None:
                expected_total, expected_pages = total, pages
                self.advertised_total, self.total_pages = total, pages
            elif total != expected_total or pages != expected_pages:
                self.has_more = True
                self.pagination_termination_reason = "advertised_pagination_changed"
                break

            for item in rows:
                job = self._parse_job(item)
                if job is None or job["source_job_id"] in seen:
                    continue
                seen.add(job["source_job_id"])
                jobs.append(job)

            if page >= pages:
                self.pagination_complete = len(seen) == total
                self.has_more = False
                self.pagination_termination_reason = (
                    "api_total_pages_and_count_reached"
                    if self.pagination_complete
                    else "api_total_count_mismatch"
                )
                break
            if not rows:
                self.has_more = True
                self.pagination_termination_reason = "empty_page_before_total"
                break
        else:
            self.has_more = True
            self.pagination_termination_reason = "max_pages_reached"

        return jobs
