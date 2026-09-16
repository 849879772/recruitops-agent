"""深德科官方招聘 API 爬虫。"""

import logging
import re

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class DekeInfoCrawler(BaseCrawler):
    API_URL = "https://dekeinfo.com/ow/recruitPost/recruitPostPageList"
    PAGE_SIZE = 100
    JD_RAW_LIMIT = 12000
    _CURRENT_COHORT = re.compile(r"(?:2027\s*届|27\s*届)", re.I)
    _INTERNSHIP_REQUIREMENT = re.compile(
        r"每周.{0,12}实习|连续实习|长期实习|实习不少于|实习\s*\d+\s*个?月",
        re.I,
    )

    def _request_jobs(self, session: requests.Session) -> dict:
        response = session.post(
            self.API_URL,
            json={"pageNum": 1, "pageSize": self.PAGE_SIZE},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://www.dekeinfo.com",
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
            logger.error("[%s] 深德科招聘接口失败: %s", self.company_name, exc)
            self.pagination_termination_reason = "api_failed"
            return []

        data = payload.get("data") or {}
        rows = data.get("records") or []
        total = int(data.get("total") or 0)
        pages = int(data.get("pages") or 0)
        if pages > 1 or len(rows) != total:
            logger.error(
                "[%s] 深德科接口未完整返回: %d/%d, pages=%d",
                self.company_name,
                len(rows),
                total,
                pages,
            )
            self.pagination_termination_reason = "api_total_mismatch"
            return []

        jobs = []
        for row in rows:
            if int(row.get("tecruitmentType") or 0) != 2:
                continue
            title = str(row.get("recruitPostName") or "").strip()
            job_id = str(row.get("recruitPostId") or "").strip()
            duty = str(row.get("jobDescription") or "").strip()
            requirement = str(row.get("jobRequirements") or "").strip()
            evidence = "\n".join([title, duty, requirement])
            if not title or not job_id or not self._CURRENT_COHORT.search(evidence):
                continue
            if self._INTERNSHIP_REQUIREMENT.search(evidence):
                continue
            jobs.append(self._make_job(
                title=title,
                city=str(row.get("workPlace") or row.get("cityName") or "").strip(),
                job_type="校招",
                jd_url=f"{self.careers_url.split('#')[0]}#job-{job_id}",
                jd_raw=(
                    f"岗位职责\n{duty}\n任职要求\n{requirement}"
                )[: self.JD_RAW_LIMIT],
                published_at=str(row.get("releaseTime") or "").strip(),
                link_kind="list",
                campaign_text="深德科2027届校园招聘",
            ))

        self.pagination_complete = True
        self.pagination_termination_reason = "api_total_reached"
        return jobs
