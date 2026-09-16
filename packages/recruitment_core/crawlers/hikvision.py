"""海康威视校招爬虫 —— 自建站 campushr.hikvision.com，走公开 JSON API。

校招职位列表由 SPA 异步加载（render DOM 抓不到），但其搜索 API 公开免鉴权：
    POST /api/search/crsPositionSearch/getPositionByQuery
    body: {"pageNum":N, "pageSize":50, "jobNature":"应届生"}
    resp: data.list[]（postStd/batchPositionName=职位名, workPlace=地点, postContent=JD,
          id=唯一标识）+ data.hasNextPage 分页。
故直接 requests 调 API 翻页，比 Playwright 渲染更快更稳。
"""
import logging

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class HikvisionCrawler(BaseCrawler):
    API = "https://campushr.hikvision.com/api/search/crsPositionSearch/getPositionByQuery"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 12000
    JOB_NATURE = "应届生"  # 只要正式校招，不要实习生（站点按 jobNature 分）

    def fetch(self) -> list[dict]:
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://campushr.hikvision.com/school",
            "User-Agent": "Mozilla/5.0",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            body = {"pageNum": page, "pageSize": self.PAGE_SIZE, "jobNature": self.JOB_NATURE}
            try:
                resp = requests.post(self.API, json=body, headers=headers, timeout=30)
                data = resp.json().get("data") or {}
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 海康 API 第%d页失败: %s", self.company_name, page, e)
                break
            for x in data.get("list", []):
                title = x.get("batchPositionName") or x.get("postStdName") or x.get("postAdName") or ""
                if not title or len(title) < 2:
                    continue
                jid = str(x.get("id") or "")
                if jid in seen:
                    continue
                seen.add(jid)
                city = (x.get("workPlace") or x.get("addr") or "")[:40]
                duties = str(x.get("postContent") or "").strip()
                requirements = str(x.get("postRequire") or "").strip()
                jd_raw = "\n".join(
                    part for part in ["岗位职责", duties, "任职要求", requirements] if part
                )[:self.JD_RAW_LIMIT]
                jd_url = f"https://campushr.hikvision.com/school?positionId={jid}"
                jobs.append(self._make_job(title=title, city=city, jd_url=jd_url, jd_raw=jd_raw))
            if not data.get("hasNextPage"):
                break

        logger.info("[%s] 海康 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
