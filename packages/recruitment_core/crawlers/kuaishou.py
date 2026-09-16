"""快手校招爬虫 —— 自建站 campus.kuaishou.cn，走公开 JSON API。

campus.kuaishou.cn 校园招聘站，职位列表有公开（/open/）免鉴权 API：
    POST https://campus.kuaishou.cn/recruit/campus/e/api/v1/open/positions/simple
    body: {"pageNum":N, "pageSize":100}   # 不传 recruitSubProjectCodes = 全部校招
                                          # （含应届+实习；实习由 reporter 按标题隐藏）
    resp: result.list[]（name=职位名, code=唯一标识, workLocationDicts[].name=地点,
          description/positionDemand=JD）+ result.pages/total。
不绑定年份相关的 recruitSubProjectCode，CI 跨年仍稳。
"""
import logging
import time

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class KuaishouCrawler(BaseCrawler):
    API = "https://campus.kuaishou.cn/recruit/campus/e/api/v1/open/positions/simple"
    DETAIL = "https://campus.kuaishou.cn/recruit/campus/e/?#/campus/jobDetail/"
    PAGE_SIZE = 100
    MAX_PAGES = 15
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Referer": "https://campus.kuaishou.cn/recruit/campus/e/",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            body = {"pageNum": page, "pageSize": self.PAGE_SIZE}
            result = None
            for attempt in range(3):
                try:
                    resp = requests.post(self.API, json=body, headers=headers, timeout=30)
                    result = resp.json().get("result") or {}
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 快手 API 第%d页第%d次失败: %s",
                                   self.company_name, page, attempt + 1, e)
                    time.sleep(1.5 * (attempt + 1))
            if result is None:
                break
            plist = result.get("list") or []
            if not plist:
                break
            for x in plist:
                code = str(x.get("code") or x.get("id") or "")
                if not code or code in seen:
                    continue
                seen.add(code)
                title = (x.get("name") or "").strip()
                if not title or len(title) < 2:
                    continue
                city = "、".join(
                    d.get("name", "") for d in (x.get("workLocationDicts") or []) if d.get("name")
                )[:40]
                duties = str(x.get("description") or "").strip()
                requirements = str(x.get("positionDemand") or "").strip()
                jd_parts = []
                if duties:
                    jd_parts.extend(["岗位职责", duties])
                if requirements:
                    jd_parts.extend(["任职要求", requirements])
                jd_raw = "\n".join(jd_parts)[:self.JD_RAW_LIMIT]
                jobs.append(self._make_job(title=title, city=city,
                                           jd_url=f"{self.DETAIL}{code}", jd_raw=jd_raw))
            if page >= (result.get("pages") or 0):
                break
            time.sleep(0.3)

        logger.info("[%s] 快手 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
