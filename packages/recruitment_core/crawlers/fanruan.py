"""帆软校园招聘官方接口爬虫。"""

import logging

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class FanruanCrawler(BaseCrawler):
    API_URL = "https://join.fanruan.com/campus"
    JD_RAW_LIMIT = 12000
    MAX_PAGES = 20

    def _request_page(self, session: requests.Session, page: int) -> dict:
        response = session.post(
            self.API_URL,
            data={"filter": "1", "page": str(page), "w": ""},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.careers_url,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                ),
            },
            timeout=25,
        )
        response.raise_for_status()
        return response.json()

    def fetch(self) -> list[dict]:
        session = requests.Session()
        jobs: list[dict] = []
        seen: set[str] = set()
        expected_total: int | None = None
        expected_pages: int | None = None
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        for page in range(1, self.MAX_PAGES + 1):
            try:
                payload = self._request_page(session, page)
            except Exception as exc:
                logger.error("[%s] 帆软接口第 %d 页失败: %s", self.company_name, page, exc)
                self.pagination_termination_reason = f"api_failed_page_{page}"
                return []

            if expected_total is None:
                expected_total = int(payload.get("dataTotal") or 0)
                expected_pages = int(payload.get("pageTotal") or 0)

            rows = payload.get("list") or []
            if not rows:
                break
            for row in rows:
                job_id = str(row.get("id") or "").strip()
                title = str(row.get("job_name") or "").strip()
                if not job_id or not title or job_id in seen:
                    continue
                seen.add(job_id)
                duty = str(row.get("duty") or "").strip()
                requirement = str(row.get("requirement") or "").strip()
                jd_parts = []
                if duty:
                    jd_parts.extend(["岗位职责", duty])
                if requirement:
                    jd_parts.extend(["任职要求", requirement])
                jobs.append(self._make_job(
                    title=title,
                    city=str(row.get("base") or "").strip(),
                    job_type=str(row.get("mode") or "校招").strip(),
                    jd_url=f"{self.API_URL}#job-{job_id}",
                    jd_raw="\n".join(jd_parts)[: self.JD_RAW_LIMIT],
                    link_kind="list",
                    campaign_text="帆软2027届校园招聘",
                ))

            if expected_pages is not None and page >= expected_pages:
                break

        self.pagination_complete = expected_total is not None and len(jobs) == expected_total
        self.pagination_termination_reason = (
            "api_total_reached" if self.pagination_complete else "api_total_mismatch"
        )
        if not self.pagination_complete:
            logger.error(
                "[%s] 帆软接口结果不完整: %d/%s",
                self.company_name,
                len(jobs),
                expected_total if expected_total is not None else "未知",
            )
            return []
        return jobs
