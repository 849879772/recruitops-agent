import logging
import math
from urllib.parse import parse_qs, urlparse

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class NetEaseCrawler(BaseCrawler):
    """NetEase campus jobs from campus.163.com project position pages."""

    API = "https://campus.163.com/api/campuspc/position/getJobList"
    PAGE_SIZE = 50
    MAX_PAGES = 5
    JD_RAW_LIMIT = 12000

    def _project_id(self) -> str:
        parsed = urlparse(self.careers_url)
        params = parse_qs(parsed.query)
        if params.get("projectId"):
            return params["projectId"][0]
        if params.get("id"):
            return params["id"][0]
        return "69"

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        project_id = self._project_id()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": self.careers_url,
            "Accept": "application/json, text/plain, */*",
        }
        jobs, seen = [], set()
        page, total_pages = 1, 1
        while page <= min(total_pages, self.MAX_PAGES):
            params = {
                "pageSize": self.PAGE_SIZE,
                "currentPage": page,
                "projectId": project_id,
            }
            try:
                resp = requests.get(self.API, params=params, headers=headers, timeout=20)
                resp.raise_for_status()
                data = (resp.json().get("data") or {})
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] NetEase position api failed page=%s: %s", self.company_name, page, e)
                self.fetch_failed = True
                self.pagination_termination_reason = "page_request_failed"
                break

            items = data.get("list") or []
            total = int(data.get("total") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            self.pages_seen = page
            self.total_pages = total_pages
            self.advertised_total = total
            for item in items:
                title = (item.get("positionName") or "").strip()
                job_id = item.get("id") or title
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                duties = str(item.get("positionDescription") or "").strip()
                requirements = str(item.get("positionRequirement") or "").strip()
                jd_raw = "\n".join(x for x in [
                    item.get("positionTypeName") or "",
                    item.get("firstBuName") or "",
                    "岗位职责", duties,
                    "任职要求", requirements,
                ] if x)
                jobs.append(self._make_job(
                    title=title,
                    city=(item.get("workPlaceName") or "")[:80],
                    jd_url=f"https://campus.163.com/app/job/detail/{job_id}?projectId={project_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                ))
            if not items:
                self.pagination_complete = page >= total_pages or total == 0
                self.pagination_termination_reason = (
                    "empty_terminal_page" if self.pagination_complete else "empty_page_before_total"
                )
                break
            if page >= total_pages:
                self.pagination_complete = len(seen) >= total
                self.pagination_termination_reason = (
                    "advertised_total_reached"
                    if self.pagination_complete
                    else "advertised_total_mismatch"
                )
                break
            page += 1
        if not self.pagination_complete and self.total_pages is not None:
            self.has_more = self.pages_seen < self.total_pages
            if self.pages_seen >= self.MAX_PAGES and self.has_more:
                self.pagination_termination_reason = "max_pages_reached"
        logger.info("[%s] NetEase fetched %d jobs", self.company_name, len(jobs))
        return jobs
