"""量智开物官方 2027 届校园招聘页爬虫。"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler


_SPACE_RE = re.compile(r"[ \t\xa0]+")
_TITLE_PREFIX_RE = re.compile(r"^[^\w\u4e00-\u9fff]+")
_CITY_RE = re.compile(r"工作地点[：:]\s*([^\n]+)")


def _clean_lines(value: object) -> str:
    lines = []
    for line in str(value or "").replace("\r", "").split("\n"):
        cleaned = _SPACE_RE.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


class IqiTekCrawler(BaseCrawler):
    """只解析官网招聘区域，排除单独列出的科研实习生岗位。"""

    EXPECTED_TOTAL = 3
    CAMPAIGN_TEXT = "量智开物2027届校园招聘"

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.expected_total = self.EXPECTED_TOTAL
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_count = 0
        self.detail_complete = False

    def fetch(self) -> list[dict]:
        response = self._get(self.careers_url, timeout=30, verify=False)
        if response is None:
            self.pagination_termination_reason = "request_failed"
            return []
        response.encoding = response.apparent_encoding or response.encoding
        soup = BeautifulSoup(response.text, "html.parser")
        careers = soup.select_one("#careers")
        if careers is None or "2027 届校园招聘" not in careers.get_text(" ", strip=True):
            self.pagination_termination_reason = "campaign_evidence_missing"
            return []

        city_match = _CITY_RE.search(_clean_lines(careers.get_text("\n", strip=True)))
        city = city_match.group(1).strip() if city_match else ""
        jobs = []
        for card in careers.select("details.job-card"):
            summary = card.select_one("summary.job-summary > span")
            body = card.select_one(".job-body")
            if summary is None or body is None:
                continue
            title = _TITLE_PREFIX_RE.sub("", summary.get_text(" ", strip=True)).strip()
            if not title or "实习" in title:
                continue
            jd_raw = _clean_lines(body.get_text("\n", strip=True))
            if "岗位职责" not in jd_raw or "任职要求" not in jd_raw:
                continue
            source_id = hashlib.sha256(f"{title}\n{jd_raw}".encode("utf-8")).hexdigest()[:24]
            job = self._make_job(
                title=title,
                city=city,
                job_type="校招",
                jd_url=f"{self.careers_url}#position-{source_id}",
                jd_raw=jd_raw,
                link_kind="list",
                campaign_text=self.CAMPAIGN_TEXT,
            )
            job.update(
                {
                    "source_job_id": source_id,
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "cohort_source": "官方2027届校园招聘页",
                    "cohort_evidence": "量智开物 2027 届校园招聘",
                    "recruitment_track": "formal",
                }
            )
            jobs.append(job)

        self.detail_count = len(jobs)
        self.detail_complete = len(jobs) == self.EXPECTED_TOTAL
        self.pagination_complete = len(jobs) == self.EXPECTED_TOTAL
        self.pagination_termination_reason = (
            "single_page_complete" if self.pagination_complete else "expected_total_mismatch"
        )
        return jobs
