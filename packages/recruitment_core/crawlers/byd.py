import logging
import math

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class BYDCrawler(BaseCrawler):
    """BYD campus jobs from the portal position list endpoint."""

    API = "https://job.byd.com/portal/api/portal-api/position/queryList"
    PAGE_SIZE = 100
    MAX_PAGES = 20
    JD_RAW_LIMIT = 800

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://job.byd.com/portal/pc/",
            "Content-Type": "application/json;charset=UTF-8",
        }
        jobs, seen = [], set()
        page, total_pages = 0, 1
        while page < min(total_pages, self.MAX_PAGES):
            payload = {
                "positionTypeArr": [],
                "positionProvinceArr": [],
                "positionCityArr": [],
                "positionOrgArr": [],
                "vagueCondition": "",
                "searchType": 1,
                "zpType": "00252",
                "pageNum": page * self.PAGE_SIZE,
                "pageSize": self.PAGE_SIZE,
            }
            try:
                resp = requests.post(self.API, json=payload, headers=headers, timeout=20)
                resp.raise_for_status()
                data = (resp.json().get("data") or {})
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] BYD position api failed page=%s: %s", self.company_name, page, e)
                break

            items = data.get("data") or []
            total = int(data.get("total") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            for item in items:
                title = (item.get("positionName") or "").strip()
                job_id = item.get("id") or item.get("positionCode") or title
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                jd_raw = "\n".join(
                    x for x in [
                        item.get("fatherOrgAliasName") or "",
                        item.get("orgAliasName") or "",
                        item.get("positionTypeName") or "",
                    ] if x
                )
                jobs.append(self._make_job(
                    title=title,
                    city=(item.get("city") or item.get("province") or "")[:80],
                    jd_url=f"https://job.byd.com/portal/pc/#/social/socialPositionDetails?id={job_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                    published_at=(item.get("createTime") or "")[:10],
                ))
            if not items:
                break
            page += 1
        logger.info("[%s] BYD fetched %d jobs", self.company_name, len(jobs))
        return jobs
