import logging
import math

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class OppoCrawler(BaseCrawler):
    """OPPO campus jobs from the public openapi position endpoint."""

    API = "https://careers.oppo.com/openapi/position/pageNew"
    PAGE_SIZE = 100
    MAX_PAGES = 5
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://careers.oppo.com/university/oppo/recruitment/post",
            "Content-Type": "application/json",
            "Tenant-Id": "1000",
        }
        jobs, seen = [], set()
        page, total_pages = 1, 1
        while page <= min(total_pages, self.MAX_PAGES):
            payload = {
                "pageNum": page,
                "pageSize": self.PAGE_SIZE,
                "positionName": "",
                "projectList": [],
                "positionTypeList": [],
                "workCityCodeList": [],
            }
            try:
                resp = requests.post(self.API, json=payload, headers=headers, timeout=20)
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") or {}
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] OPPO position api failed page=%s: %s", self.company_name, page, e)
                break

            items = data.get("records") or []
            total = int(data.get("total") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            for item in items:
                title = (item.get("positionName") or item.get("projectPositionName") or "").strip()
                job_id = item.get("idProjPosition") or item.get("idRecruitPosition") or title
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                duties = str(item.get("positionDesc") or "").strip()
                requirements = str(item.get("positionRequire") or "").strip()
                jd_raw = "\n".join(x for x in [
                    item.get("projectName") or "",
                    item.get("positionTypeName") or "",
                    "岗位职责", duties,
                    "任职要求", requirements,
                ] if x)
                jobs.append(self._make_job(
                    title=title,
                    city=(item.get("workCityName") or "")[:80],
                    jd_url=f"https://careers.oppo.com/university/oppo/campus/post/{job_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                    published_at=item.get("releaseTime") or "",
                ))
            if not items:
                break
            page += 1
        logger.info("[%s] OPPO fetched %d jobs", self.company_name, len(jobs))
        return jobs
