import logging
import math

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class SFCrawler(BaseCrawler):
    """SF Express campus jobs from the public campus position endpoint."""

    API = "https://campus.sf-express.com/api/web/position/query"
    PAGE_SIZE = 50
    MAX_PAGES = 10
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://campus.sf-express.com/positionList",
        }
        jobs, seen = [], set()
        page, total_pages = 1, 1
        while page <= min(total_pages, self.MAX_PAGES):
            params = {"pageNum": page, "pageSize": self.PAGE_SIZE}
            try:
                resp = requests.get(self.API, params=params, headers=headers, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] SF position api failed page=%s: %s", self.company_name, page, e)
                break

            items = data.get("list") or []
            total = int(data.get("total") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            for item in items:
                title = (item.get("positionName") or "").strip()
                job_id = item.get("id") or title
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                duties = str(item.get("postDuty") or "").strip()
                requirements = str(item.get("jobRequirement") or "").strip()
                jd_raw = "\n".join(x for x in [
                    item.get("orgSourceName") or "",
                    item.get("positionTypeName") or "",
                    item.get("internTypeName") or "",
                    "岗位职责", duties,
                    "任职要求", requirements,
                ] if x)
                jobs.append(self._make_job(
                    title=title,
                    city=(item.get("demandCity") or "")[:80],
                    jd_url=f"https://campus.sf-express.com/#/postDetail/{job_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                    published_at=(item.get("createDate") or "")[:10],
                ))
            if not items:
                break
            page += 1
        logger.info("[%s] SF fetched %d jobs", self.company_name, len(jobs))
        return jobs
