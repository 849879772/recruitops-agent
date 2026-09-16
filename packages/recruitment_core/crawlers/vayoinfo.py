"""望友科技官方 2027 届校招文章爬虫。"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler


_POSITION_RE = re.compile(r"^岗位[一二三四五六七八九十\d]+[：:]\s*(.+)$")
_CITY_RE = re.compile(r"工作地点[：:]?\s*([^\n]+)")


class VayoInfoCrawler(BaseCrawler):
    """从官方文章正文提取两个岗位，忽略全站产品和导航文字。"""

    EXPECTED_TOTAL = 2
    CAMPAIGN_TEXT = "望友科技2027届校招"

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.expected_total = self.EXPECTED_TOTAL
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_count = 0
        self.detail_complete = False

    @staticmethod
    def _title(value: str) -> str:
        title = re.split(r"[（(](?:薪资|博士岗)", value, maxsplit=1)[0].strip()
        if "博士岗" in value:
            title += "（博士岗）"
        return title

    def fetch(self) -> list[dict]:
        response = self._get(self.careers_url, timeout=30, verify=False)
        if response is None:
            self.pagination_termination_reason = "request_failed"
            return []
        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.select_one(".newshowtxt")
        if content is None:
            self.pagination_termination_reason = "article_body_missing"
            return []
        text = content.get_text("\n", strip=True).replace("\r", "")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if "2027届校招" not in f"{page_title}\n{text}":
            self.pagination_termination_reason = "campaign_evidence_missing"
            return []
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.split("\n")]
        lines = [line for line in lines if line]

        starts = [index for index, line in enumerate(lines) if _POSITION_RE.match(line)]
        jobs = []
        for offset, start in enumerate(starts):
            end = starts[offset + 1] if offset + 1 < len(starts) else len(lines)
            for index in range(start + 1, end):
                if lines[index].startswith("三、投递方式"):
                    end = index
                    break
            match = _POSITION_RE.match(lines[start])
            title = self._title(match.group(1)) if match else ""
            jd_raw = "\n".join(lines[start + 1 : end]).strip()
            if not title or "任职要求" not in jd_raw or len(jd_raw) < 80:
                continue
            city_match = _CITY_RE.search(jd_raw)
            city = city_match.group(1).strip() if city_match else ""
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
                    "cohort_source": "官方2027届校招文章",
                    "cohort_evidence": "望友科技2027届校招通道开启",
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
