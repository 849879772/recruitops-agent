"""Pinduoduo campus crawler backed by the site's public JSON APIs."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_API_ROOT = "https://careers.pddglobalhr.com/api/careers/api/recruit/position"
_LIST_API = f"{_API_ROOT}/list"
_DETAIL_API = f"{_API_ROOT}/detail"
_DETAIL_PAGE = "https://careers.pddglobalhr.com/campus/grad/detail?positionId={position_id}"
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://careers.pddglobalhr.com/campus/grad",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    ),
}


class PDDCrawler(BaseCrawler):
    PAGE_SIZE = 100
    MAX_PAGES = 20

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_evidence()

    def _reset_evidence(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False

    @staticmethod
    def _as_int(value: object) -> int | None:
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _as_bool(value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        return None

    @classmethod
    def _result_int(cls, result: dict, *keys: str) -> int | None:
        for key in keys:
            if key in result:
                parsed = cls._as_int(result[key])
                if parsed is not None:
                    return parsed
        return None

    @classmethod
    def _no_pagination_contract(cls, result: dict) -> bool:
        pagination = result.get("pagination")
        if pagination is False:
            return True
        if isinstance(pagination, dict) and cls._as_bool(pagination.get("enabled")) is False:
            return True
        for key in ("paginationEnabled", "hasPagination", "paginated", "isPaginated", "pageable"):
            if key in result and cls._as_bool(result[key]) is False:
                return True
        page_info = result.get("pageInfo")
        return isinstance(page_info, dict) and cls._as_bool(page_info.get("enabled")) is False

    def _post_result(self, session: requests.Session, url: str, body: dict):
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = session.post(url, json=body, timeout=25)
                response.raise_for_status()
                payload = response.json()
                if payload.get("success"):
                    return payload.get("result")
                raise RuntimeError(payload.get("errorMsg") or "official API returned success=false")
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning("[%s] 拼多多接口请求失败 %s: %s", self.company_name, url, exc)
                    return None
                time.sleep(0.5 * attempt)
        return None

    @staticmethod
    def _published_at(value) -> str:
        try:
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return ""

    @staticmethod
    def _jd_text(detail: dict, fallback: dict) -> str:
        duty = str(detail.get("jobDuty") or fallback.get("jobDuty") or "").strip()
        requirement = str(detail.get("serveRequirement") or "").strip()
        bonus = str(detail.get("bonus") or "").strip()
        parts = []
        if duty:
            parts.extend(["岗位职责", duty])
        if requirement:
            parts.extend(["任职要求", requirement])
        if bonus:
            parts.extend(["加分项", bonus])
        return "\n".join(parts)[:12000]

    def fetch(self) -> list[dict]:
        self._reset_evidence()
        session = requests.Session()
        session.headers.update(_HEADERS)
        rows: list[dict] = []
        seen_list_ids: set[str] = set()
        duplicate_id = False
        invalid_row = False
        total = None
        total_pages = None
        no_pagination_contract = False
        page = 1
        while page <= self.MAX_PAGES:
            result = self._post_result(
                session,
                _LIST_API,
                {"page": page, "pageSize": self.PAGE_SIZE, "t": None},
            )
            if not isinstance(result, dict):
                self.fetch_failed = True
                self.has_more = True
                self.pagination_termination_reason = "page_request_failed"
                break

            page_rows = result.get("list")
            if not isinstance(page_rows, list):
                self.fetch_failed = True
                self.has_more = True
                self.pagination_termination_reason = "list_payload_invalid"
                break

            self.pages_seen = page
            page_total = self._as_int(
                result.get("total") if "total" in result else result.get("totalCount")
            )
            if total is None:
                total = page_total
            elif page_total is not None and page_total != total:
                self.has_more = True
                self.pagination_termination_reason = "total_changed_between_pages"
                break

            response_page = self._result_int(
                result, "pageNo", "page_no", "currentPage", "current_page", "page"
            )
            if response_page is not None and response_page != page:
                self.has_more = True
                self.pagination_termination_reason = "pagination_page_mismatch"
                break

            response_page_size = self._result_int(result, "pageSize", "page_size")
            reported_pages = self._result_int(
                result, "totalPages", "total_pages", "pageCount", "page_count", "pages"
            )
            page_total_pages = (
                reported_pages
                if reported_pages is not None and reported_pages > 0
                else (
                    math.ceil(total / response_page_size)
                    if total is not None and response_page_size
                    else None
                )
            )
            if total_pages is None:
                total_pages = page_total_pages
            elif page_total_pages is not None and page_total_pages != total_pages:
                self.has_more = True
                self.pagination_termination_reason = "page_count_changed_between_pages"
                break

            no_pagination_contract = no_pagination_contract or self._no_pagination_contract(result)
            reported_has_more = self._as_bool(
                result.get("hasMore") if "hasMore" in result else result.get("has_more")
            )
            inferred_has_more = (
                False
                if no_pagination_contract and total_pages is None
                else True if total_pages is None else page < total_pages
            )
            self.has_more = (
                reported_has_more if reported_has_more is not None else inferred_has_more
            )
            pagination_conflict = (
                reported_has_more is not None
                and total_pages is not None
                and ((reported_has_more and page >= total_pages) or (
                    not reported_has_more and page < total_pages
                ))
            )

            for row in page_rows:
                if not isinstance(row, dict):
                    invalid_row = True
                    continue
                position_id = str(row.get("id") or "").strip()
                title = str(row.get("name") or "").strip()
                if not position_id or not title:
                    invalid_row = True
                    continue
                if position_id in seen_list_ids:
                    duplicate_id = True
                    continue
                seen_list_ids.add(position_id)
                rows.append(row)

            if total is None:
                if no_pagination_contract and not self.has_more:
                    total = len(seen_list_ids)
                    total_pages = 1
                    self.advertised_total = total
                    self.total_pages = total_pages
                    if pagination_conflict or duplicate_id or invalid_row:
                        self.pagination_termination_reason = (
                            "pagination_evidence_conflict"
                            if pagination_conflict
                            else "duplicate_job_id" if duplicate_id else "invalid_row"
                        )
                    else:
                        self.pagination_complete = True
                        self.pagination_termination_reason = "explicit_no_pagination_contract"
                else:
                    self.has_more = (
                        reported_has_more
                        if reported_has_more is not None
                        else bool(page_rows) or self.has_more
                    )
                    self.pagination_termination_reason = (
                        "pagination_evidence_conflict"
                        if pagination_conflict
                        else "missing_total"
                    )
                break

            self.advertised_total = total
            if total_pages is not None:
                self.total_pages = total_pages
            if pagination_conflict:
                self.pagination_termination_reason = "pagination_evidence_conflict"
                self.has_more = True
                break

            if len(seen_list_ids) == total:
                if duplicate_id:
                    self.pagination_termination_reason = "duplicate_job_id"
                elif invalid_row:
                    self.pagination_termination_reason = "invalid_row"
                elif reported_has_more is True:
                    self.pagination_termination_reason = "pagination_evidence_conflict"
                    self.has_more = True
                else:
                    self.pagination_complete = True
                    self.has_more = False
                    if self.total_pages is None:
                        self.total_pages = page
                    self.pagination_termination_reason = "api_total_reached"
                break

            if not page_rows and total > 0:
                self.has_more = True
                if self.total_pages is None:
                    self.total_pages = page
                self.pagination_termination_reason = "empty_page_before_total"
                break

            if reported_has_more is False:
                self.has_more = False
                if self.total_pages is None:
                    self.total_pages = page
                self.pagination_termination_reason = "advertised_total_mismatch"
                break

            if self.total_pages is not None and page >= self.total_pages:
                if duplicate_id:
                    self.pagination_termination_reason = "duplicate_job_id"
                elif invalid_row:
                    self.pagination_termination_reason = "invalid_row"
                else:
                    self.pagination_termination_reason = "advertised_total_mismatch"
                break
            page += 1

        if not self.pagination_complete and self.pagination_termination_reason == "not_started":
            self.has_more = True
            self.pagination_termination_reason = "max_pages_reached"

        jobs = []
        seen_ids = set()
        for row in rows:
            position_id = str(row.get("id") or "").strip()
            title = str(row.get("name") or "").strip()
            if not position_id or not title or position_id in seen_ids:
                continue
            seen_ids.add(position_id)

            detail = self._post_result(
                session,
                _DETAIL_API,
                {"id": position_id, "t": None},
            )
            detail = detail if isinstance(detail, dict) else {}
            year = str(detail.get("graduationYear") or row.get("graduationYear") or "").strip()
            recruit_type = str(
                detail.get("recruitTypeName") or row.get("recruitTypeName") or ""
            ).strip()
            job_type = " ".join(
                part for part in ("校招", "正式", f"{year}届" if year else "", recruit_type)
                if part
            )
            detail_url = str(detail.get("shareUrl") or "").strip() or _DETAIL_PAGE.format(
                position_id=position_id
            )
            jobs.append(self._make_job(
                title=title,
                city=str(
                    detail.get("workLocationName") or row.get("workLocationName")
                    or row.get("workLocation") or ""
                ).strip(),
                job_type=job_type,
                jd_url=detail_url,
                jd_raw=self._jd_text(detail, row),
                published_at=self._published_at(
                    detail.get("releaseTime") or row.get("releaseTime")
                ),
                link_kind="detail",
            ))

        logger.info("[%s] 拼多多官方 API 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
