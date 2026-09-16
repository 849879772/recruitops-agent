"""Lenovo campus crawler for the public gateway API."""
import logging
import math

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class LenovoCrawler(BaseCrawler):
    API = "https://talent.lenovo.com.cn/gateway/jobBase/list"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 500

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://talent.lenovo.com.cn/position",
            "portal-type": "PC",
            "content-type": "application/json;charset=UTF-8",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            try:
                resp = requests.get(
                    self.API,
                    params={"pageNum": page, "pageSize": self.PAGE_SIZE},
                    headers=headers,
                    timeout=30,
                )
                result = resp.json().get("result") or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Lenovo API page %d failed: %s", self.company_name, page, exc)
                self.fetch_failed = True
                self.has_more = bool(
                    self.total_pages is None or self.pages_seen < self.total_pages
                )
                self.pagination_termination_reason = "page_request_failed"
                break

            self.pages_seen = page
            rows = result.get("rows") or []
            total_value = result.get("total")
            total = int(total_value) if total_value not in (None, "") else None
            if total is not None:
                if self.advertised_total is not None and total != self.advertised_total:
                    self.has_more = True
                    self.pagination_termination_reason = "total_changed_between_pages"
                    break
                self.advertised_total = total
                page_size = int(result.get("pageSize") or self.PAGE_SIZE)
                self.total_pages = max(1, math.ceil(total / max(1, page_size)))
            if not rows:
                self.pagination_complete = bool(
                    total == 0
                    or (
                        self.total_pages is not None
                        and page >= self.total_pages
                        and len(seen) == total
                    )
                )
                self.has_more = not self.pagination_complete and bool(total)
                self.pagination_termination_reason = (
                    "empty_terminal_page"
                    if self.pagination_complete
                    else "empty_page_before_total"
                )
                break
            for item in rows:
                jid = str(item.get("id") or "")
                title = item.get("jobName") or ""
                if not title or jid in seen:
                    continue
                seen.add(jid)
                city = str(item.get("workPlace") or "")[:40]
                jd_raw = str(item.get("cont") or item.get("jobDesc") or "")[: self.JD_RAW_LIMIT]
                jobs.append(
                    self._make_job(
                        title=title,
                        city=city,
                        jd_url=f"https://talent.lenovo.com.cn/position/detail?id={jid}",
                        jd_raw=jd_raw,
                    )
                )
            if total is not None and len(seen) >= total:
                self.pagination_complete = True
                self.pagination_termination_reason = "advertised_total_reached"
                break
            if self.total_pages is not None and page >= self.total_pages:
                self.has_more = len(seen) < (total or 0)
                self.pagination_termination_reason = "advertised_total_mismatch"
                break
        else:
            self.has_more = True
            self.pagination_termination_reason = "max_pages_reached"

        logger.info("[%s] Lenovo caught %d jobs", self.company_name, len(jobs))
        return jobs
