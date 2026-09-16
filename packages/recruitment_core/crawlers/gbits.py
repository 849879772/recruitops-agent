import logging
import math

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class GbitsCrawler(BaseCrawler):
    """吉比特/雷霆游戏校招：joinserver.g-bits.com 校招岗位接口。"""

    API = "https://joinserver.g-bits.com:8666/humanResource/recruitmentExtranet/ExtrannetCampusPost/queryRecuitPost"
    PAGE_SIZE = 50
    MAX_PAGES = 10
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://hr.g-bits.com/web/index.html",
            "Content-Type": "application/json",
        }
        jobs, seen = [], set()
        page, total_pages = 1, 1
        while page <= min(total_pages, self.MAX_PAGES):
            payload = {
                "currentPage": page,
                "pageSize": self.PAGE_SIZE,
                "recruitsType": "CAMPUS_RECRUITING",
                "recruitProjectId": "",
                "recruitmentType": None,
                "workPlace": None,
                "postTypes": None,
            }
            try:
                resp = requests.post(self.API, json=payload, headers=headers, timeout=20)
                resp.raise_for_status()
                data = (resp.json().get("data") or {})
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 吉比特岗位接口失败 page=%s: %s", self.company_name, page, e)
                break
            items = data.get("list") or []
            total = int(data.get("count") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            for item in items:
                job_id = item.get("id") or item.get("postName")
                title = (item.get("postName") or "").strip()
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                jd_raw = "\n".join(
                    x for x in [
                        item.get("recruitProjectName") or "",
                        item.get("gameProjectName") or "",
                        item.get("postType") or "",
                        "职位描述",
                        item.get("description") or "",
                    ] if x
                )
                jobs.append(self._make_job(
                    title=title,
                    city=(item.get("workAddress") or "")[:40],
                    jd_url=f"https://hr.g-bits.com/web/index.html#/post-web/post-detail/{job_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                ))
            if not items:
                break
            page += 1
        logger.info("[%s] 吉比特抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
