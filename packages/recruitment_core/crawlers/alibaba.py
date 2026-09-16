import logging
import math
import json
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class AlibabaCrawler(BaseCrawler):
    """阿里巴巴校园招聘：campus-talent.alibaba.com 公开岗位搜索接口。"""

    DEFAULT_HOST = "campus-talent.alibaba.com"
    DEFAULT_BATCH_ID = 100000540002
    CAMPUS_CHANNEL = "new_campus_group_official_site"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 12000

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
    def _content_value(cls, content: dict, keys: tuple[str, ...]) -> object:
        for key in keys:
            if key in content:
                return content[key]
        return None

    def _origin(self) -> str:
        host = urlparse(self.careers_url).netloc or self.DEFAULT_HOST
        if host in {"campus.alibaba.com", "talent.alibaba.com"}:
            host = self.DEFAULT_HOST
        return f"https://{host}"

    def _position_url(self, batch_id: int | None = None) -> str:
        url = f"{self._origin()}/campus/position"
        return f"{url}?batchId={batch_id}" if batch_id else url

    def _configured_batch_ids(self) -> list[int]:
        values = parse_qs(urlparse(self.careers_url).query).get("batchId") or []
        return [int(value) for value in values if str(value).isdigit()]

    def _configured_circle_code(self) -> str:
        values = parse_qs(urlparse(self.careers_url).query).get("circleCode") or []
        return str(values[0]).strip() if values and str(values[0]).strip() else ""

    def _configured_filters(self) -> dict[str, object]:
        """Translate campaign URL filters to Alibaba's position-search payload."""
        values = parse_qs(urlparse(self.careers_url).query).get("filterParams") or []
        params: dict[str, object] = {}
        if values:
            raw_filter = str(values[0]).strip()
            custom_dept = None
            if raw_filter.startswith("customDept") and len(raw_filter) > len("customDept"):
                custom_dept = raw_filter[len("customDept"):]
            elif raw_filter:
                try:
                    filters = json.loads(raw_filter)
                except (TypeError, ValueError):
                    logger.warning("[%s] 阿里系 filterParams 不是有效 JSON", self.company_name)
                else:
                    custom_dept = filters.get("customDept") if isinstance(filters, dict) else None
            if isinstance(custom_dept, list):
                custom_dept = ",".join(str(value) for value in custom_dept if str(value).strip())
            if custom_dept:
                params["customDeptCode"] = str(custom_dept)

        circle_code = self._configured_circle_code()
        if circle_code:
            params.update({
                "referralCircleCode": circle_code,
                "circleCodes": [circle_code],
            })
        return params

    def _detail_url(self, job_id: object, filters: dict[str, object]) -> str:
        """Use the current SPA detail route when a department filter is available."""
        job_id_text = quote(str(job_id), safe="")
        custom_dept = str(filters.get("customDeptCode") or "").strip()
        if custom_dept:
            query = urlencode({"deptCodes": custom_dept})
            return f"{self._origin()}/campus/position/{job_id_text}?{query}"
        # Keep the legacy route for old unfiltered Alibaba configurations.
        return f"{self._origin()}/campus/position-detail?positionId={job_id_text}"

    def _session(self) -> requests.Session:
        s = requests.Session()
        configured_batch = next(iter(self._configured_batch_ids()), None)
        landing_url = (
            self.careers_url
            if configured_batch and "/campus/position" in self.careers_url
            else self._position_url(configured_batch or self.DEFAULT_BATCH_ID)
        )
        s.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": landing_url,
            "Content-Type": "application/json",
        })
        s.get(landing_url, timeout=20)
        return s

    def _batch_ids(self, s: requests.Session) -> list[int]:
        configured = self._configured_batch_ids()
        if self._origin().endswith(self.DEFAULT_HOST):
            return configured or [self.DEFAULT_BATCH_ID]
        csrf = s.cookies.get("XSRF-TOKEN")
        if not csrf:
            return []
        try:
            resp = s.post(
                f"{self._origin()}/searchCondition/listBatch?_csrf={csrf}",
                json={"channel": self.CAMPUS_CHANNEL, "language": "zh"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 阿里系批次接口失败: %s", self.company_name, e)
            return []

        ids: list[int] = []

        def walk(obj):
            if isinstance(obj, dict):
                if obj.get("id") is not None and str(obj.get("id")).isdigit():
                    ids.append(int(obj["id"]))
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data.get("content") or data)
        return list(dict.fromkeys([*configured, *ids]))

    def fetch(self) -> list[dict]:
        self._reset_evidence()
        try:
            s = self._session()
        except Exception as exc:  # noqa: BLE001
            self.fetch_failed = True
            self.pagination_termination_reason = "session_bootstrap_failed"
            logger.warning("[%s] 阿里巴巴会话初始化失败: %s", self.company_name, exc)
            return []

        csrf = s.cookies.get("XSRF-TOKEN")
        if not csrf:
            self.fetch_failed = True
            self.pagination_termination_reason = "session_bootstrap_failed"
            logger.warning("[%s] 阿里巴巴未拿到 XSRF-TOKEN", self.company_name)
            return []

        jobs, seen = [], set()
        configured_filters = self._configured_filters()
        try:
            batch_ids = self._batch_ids(s)
        except Exception as exc:  # noqa: BLE001
            self.fetch_failed = True
            self.pagination_termination_reason = "batch_discovery_failed"
            logger.warning("[%s] 阿里系批次发现失败: %s", self.company_name, exc)
            return []
        if not batch_ids:
            self.fetch_failed = True
            self.pagination_termination_reason = "batch_discovery_failed"
            return []

        batch_states = []
        for batch_id in batch_ids:
            state = {
                "total": None,
                "total_pages": None,
                "pages_seen": 0,
                "seen": set(),
                "duplicate": False,
                "invalid": False,
                "complete": False,
                "has_more": False,
                "reason": "not_started",
            }
            page = 1
            while True:
                total_pages = state["total_pages"]
                if page > self.MAX_PAGES:
                    state["has_more"] = True
                    state["reason"] = "max_pages_reached"
                    break
                if total_pages is not None and page > total_pages:
                    state["reason"] = "pagination_not_exhausted"
                    break
                payload = {
                    "batchId": batch_id,
                    "pageIndex": page,
                    "pageSize": self.PAGE_SIZE,
                    "channel": self.CAMPUS_CHANNEL,
                    "language": "zh",
                    **configured_filters,
                }
                try:
                    resp = s.post(f"{self._origin()}/position/search?_csrf={csrf}", json=payload, timeout=20)
                    resp.raise_for_status()
                    body = resp.json()
                    content = body.get("content") if isinstance(body, dict) else None
                    if not isinstance(content, dict):
                        raise ValueError("search_content_missing")
                    items = content.get("datas")
                    if not isinstance(items, list):
                        raise ValueError("search_datas_missing")
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 阿里系岗位接口失败 batch=%s page=%s: %s",
                                   self.company_name, batch_id, page, e)
                    self.fetch_failed = True
                    state["has_more"] = True
                    state["reason"] = "page_request_failed"
                    break

                self.pages_seen += 1
                state["pages_seen"] += 1
                total = self._as_int(self._content_value(content, ("totalCount", "total")))
                if state["total"] is None:
                    state["total"] = total
                elif total is not None and total != state["total"]:
                    state["reason"] = "total_changed_between_pages"
                    state["has_more"] = True
                    break

                page_size = self._as_int(self._content_value(content, ("pageSize", "size")))
                page_size = page_size or self.PAGE_SIZE
                reported_pages = self._as_int(
                    self._content_value(content, ("totalPages", "pageCount", "pages"))
                )
                if reported_pages is not None and reported_pages > 0:
                    expected_pages = reported_pages
                elif total is not None:
                    expected_pages = max(1, math.ceil(total / page_size))
                else:
                    expected_pages = None
                if state["total_pages"] is None:
                    state["total_pages"] = expected_pages
                elif expected_pages is not None and expected_pages != state["total_pages"]:
                    state["reason"] = "page_count_changed_between_pages"
                    state["has_more"] = True
                    break

                current_page = self._as_int(
                    self._content_value(content, ("currentPage", "pageIndex", "page"))
                )
                if current_page is not None and current_page != page:
                    state["reason"] = "pagination_page_mismatch"
                    state["has_more"] = True
                    break

                reported_has_more = self._as_bool(
                    self._content_value(content, ("hasMore", "has_more"))
                )
                state["has_more"] = (
                    reported_has_more
                    if reported_has_more is not None
                    else bool(expected_pages is None or page < expected_pages)
                )
                for item in items:
                    if not isinstance(item, dict):
                        state["invalid"] = True
                        continue
                    title = (item.get("name") or "").strip()
                    raw_job_id = item.get("id")
                    job_id = str(raw_job_id).strip() if raw_job_id not in (None, "") else ""
                    if not title or not job_id:
                        state["invalid"] = True
                        continue
                    if job_id in state["seen"] or job_id in seen:
                        state["duplicate"] = True
                        continue
                    state["seen"].add(job_id)
                    seen.add(job_id)
                    city = " / ".join(item.get("workLocations") or [])
                    duties = str(item.get("description") or "").strip()
                    requirements = str(item.get("requirement") or "").strip()
                    jd_raw = "\n".join(
                        x for x in ["岗位职责", duties, "任职要求", requirements] if x
                    )
                    jobs.append(self._make_job(
                        title=title,
                        city=city[:80],
                        jd_url=self._detail_url(job_id, configured_filters),
                        jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                        published_at="",
                    ))

                if total is None:
                    state["reason"] = "missing_total"
                    state["has_more"] = bool(items)
                    break

                if state["total_pages"] is None:
                    state["reason"] = "missing_total_pages"
                    state["has_more"] = True
                    break
                if not items and page < state["total_pages"]:
                    state["reason"] = "empty_page_before_total"
                    state["has_more"] = True
                    break
                if page >= state["total_pages"]:
                    if state["has_more"]:
                        state["reason"] = "pagination_evidence_conflict"
                    elif state["duplicate"]:
                        state["reason"] = "duplicate_job_id"
                    elif state["invalid"]:
                        state["reason"] = "invalid_row"
                    elif len(state["seen"]) == state["total"]:
                        state["complete"] = True
                        state["reason"] = "api_total_reached"
                    else:
                        state["reason"] = "advertised_total_mismatch"
                    break
                page += 1

            batch_states.append(state)

        known_totals = [state["total"] for state in batch_states]
        self.advertised_total = (
            sum(known_totals) if known_totals and all(value is not None for value in known_totals) else None
        )
        known_pages = [state["total_pages"] for state in batch_states]
        self.total_pages = (
            sum(known_pages) if known_pages and all(value is not None for value in known_pages) else None
        )
        self.has_more = any(bool(state["has_more"]) for state in batch_states)
        self.pagination_complete = bool(batch_states) and all(
            bool(state["complete"]) for state in batch_states
        ) and not self.fetch_failed
        if self.pagination_complete:
            self.has_more = False
            self.pagination_termination_reason = "api_total_reached"
        else:
            self.pagination_termination_reason = next(
                (
                    str(state["reason"])
                    for state in batch_states
                    if not state["complete"] and state["reason"] != "not_started"
                ),
                next(
                    (
                        str(state["reason"])
                        for state in batch_states
                        if state["reason"] != "not_started"
                    ),
                    "pagination_incomplete",
                ),
            )
        logger.info("[%s] 阿里系抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
