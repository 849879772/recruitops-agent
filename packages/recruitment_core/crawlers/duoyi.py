"""多益网络 2027 届秋季提前批官方 API 爬虫。"""

import logging

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class DuoyiCrawler(BaseCrawler):
    API_URL = "https://xz.duoyi.com/v40/api/index/positions/jds/page"
    PAGE_SIZE = 100
    JD_RAW_LIMIT = 12000

    def _request_jobs(self, session: requests.Session) -> dict:
        response = session.get(
            self.API_URL,
            params={"recruit": 10, "pageIndex": 1, "pageSize": self.PAGE_SIZE},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.careers_url,
            },
            timeout=25,
        )
        response.raise_for_status()
        return response.json()

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        try:
            payload = self._request_jobs(requests.Session())
        except Exception as exc:
            logger.error("[%s] 多益招聘接口失败: %s", self.company_name, exc)
            self.pagination_termination_reason = "api_failed"
            return []

        data = payload.get("data") or {}
        rows = data.get("list") or []
        total = int(data.get("total") or 0)
        if len(rows) != total or total > self.PAGE_SIZE:
            logger.error(
                "[%s] 多益接口未完整返回: %d/%d",
                self.company_name,
                len(rows),
                total,
            )
            self.pagination_termination_reason = "api_total_mismatch"
            return []

        jobs = []
        for row in rows:
            natures = [str(value) for value in (row.get("outerNature") or [])]
            if any("实习" in value for value in natures):
                continue
            title = str(row.get("name") or "").strip()
            job_id = str(row.get("id") or "").strip()
            duty = str(row.get("jobResponsibility") or "").strip()
            requirement = str(row.get("jobRequirements") or "").strip()
            if not title or not job_id or not (duty or requirement):
                continue
            jobs.append(self._make_job(
                title=title,
                city="、".join(str(x) for x in (row.get("workPlaces") or []) if x),
                job_type="校招提前批",
                jd_url=f"https://xz.duoyi.com/v40/#/position-detail/{job_id}",
                jd_raw=(
                    f"岗位职责\n{duty}\n任职要求\n{requirement}"
                )[: self.JD_RAW_LIMIT],
                published_at=str(row.get("publishDate") or "").strip(),
                campaign_text="多益网络2027届校园招聘秋季提前批",
            ))

        self.pagination_complete = True
        self.pagination_termination_reason = "api_total_reached"
        return jobs
