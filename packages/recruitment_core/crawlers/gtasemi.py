"""积塔半导体官方 2027 校园招聘页爬虫。"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler


_SPACE_RE = re.compile(r"[ \t\xa0]+")
_CITY_RE = re.compile(r"地点[：:]\s*([^\n]+)")


def _clean_lines(value: object) -> str:
    lines = []
    for line in str(value or "").replace("\r", "").split("\n"):
        cleaned = _SPACE_RE.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


class GtaSemiCrawler(BaseCrawler):
    """只解析官方 2027 校招页中的八个岗位卡片。"""

    EXPECTED_TOTAL = 8
    CAMPAIGN_TEXT = "积塔半导体2027届校园招聘"

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

        soup = BeautifulSoup(response.text, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        if "2027届校园招聘" not in page_text:
            self.pagination_termination_reason = "campaign_evidence_missing"
            return []

        jobs = []
        seen: set[str] = set()
        for card in soup.select(".s_g1li"):
            title_node = card.select_one(".s_g1litoptit p")
            title = _SPACE_RE.sub(" ", title_node.get_text(" ", strip=True)).strip() if title_node else ""
            if not title or title in seen:
                continue

            raw = _clean_lines(card.get_text("\n", strip=True))
            raw = raw.split("\n联系方式", 1)[0].strip()
            if "岗位职责" not in raw or "岗位要求" not in raw:
                continue
            city_match = _CITY_RE.search(raw)
            city = city_match.group(1).strip() if city_match else ""
            source_id = hashlib.sha256(f"{title}\n{raw}".encode("utf-8")).hexdigest()[:24]
            job = self._make_job(
                title=title,
                city=city,
                job_type="校招",
                jd_url=f"{self.careers_url}#position-{source_id}",
                jd_raw=raw,
                link_kind="list",
                campaign_text=self.CAMPAIGN_TEXT,
            )
            job.update(
                {
                    "source_job_id": source_id,
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "cohort_source": "官方2027届校园招聘页",
                    "cohort_evidence": "2027届校园招聘",
                    "recruitment_track": "formal",
                }
            )
            seen.add(title)
            jobs.append(job)

        self.detail_count = len(jobs)
        self.detail_complete = len(jobs) == self.EXPECTED_TOTAL
        self.pagination_complete = len(jobs) == self.EXPECTED_TOTAL
        self.pagination_termination_reason = (
            "single_page_complete" if self.pagination_complete else "expected_total_mismatch"
        )
        return jobs
