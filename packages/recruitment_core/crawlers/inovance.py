"""汇川技术新版招聘官网爬虫（recruit.inovance.com）。"""

import logging

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class InovanceRecruitCrawler(BaseCrawler):
    """Fetch campus jobs from Inovance's public portal API."""

    API_URL = "https://recruit.inovance.com/prod-portal-api/position/ad/search"
    PORTAL_ID = "019daf7d-4d1a-7634-87af-1f089498b6f2"
    PAGE_SIZE = 100

    def fetch(self) -> list[dict]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": "https://recruit.inovance.com",
            "Referer": "https://recruit.inovance.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "X-Portal-Id": self.PORTAL_ID,
        }
        jobs: list[dict] = []
        seen_ids = set()
        page_num = 1

        while page_num <= 20:
            payload = {
                "pageNum": page_num,
                "pageSize": self.PAGE_SIZE,
                "keyword": "",
                "recruitTypes": [1],
                "sortBy": "recommended",
            }
            try:
                response = requests.post(
                    self.API_URL, json=payload, headers=headers, timeout=30,
                )
                response.raise_for_status()
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("[%s] 汇川官网接口请求失败: %s", self.company_name, exc)
                break

            if body.get("code") != 200:
                logger.warning("[%s] 汇川官网接口返回异常: %s", self.company_name, body.get("message"))
                break
            data = body.get("data") or {}
            records = data.get("records") or []
            if not records:
                break

            for record in records:
                ad_id = str(record.get("adId") or "").strip()
                title = str(record.get("adJobName") or "").strip()
                if not ad_id or not title or ad_id in seen_ids:
                    continue
                seen_ids.add(ad_id)
                cities = [
                    str(location.get("name") or "").strip()
                    for location in (record.get("workLocation") or [])
                    if isinstance(location, dict) and location.get("name")
                ]
                duties = str(record.get("jobDescription") or "").strip()
                requirements = str(record.get("jobRequirement") or "").strip()
                jd_raw = "\n".join(
                    part for part in ("岗位职责", duties, "任职要求", requirements) if part
                )[:12000]
                jobs.append(self._make_job(
                    title=title,
                    city=" / ".join(cities),
                    jd_url=f"https://recruit.inovance.com/#/jobs/{ad_id}",
                    jd_raw=jd_raw,
                    published_at=(record.get("publishTime") or "")[:10],
                ))

            if not data.get("hasMore") or len(records) < self.PAGE_SIZE:
                break
            page_num += 1

        logger.info("[%s] 汇川官网抓到 %d 个校园岗位", self.company_name, len(jobs))
        return jobs
