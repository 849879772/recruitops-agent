"""百度校招爬虫 —— 自建站 talent.baidu.com，走公开 JSON API。

talent.baidu.com/jobs/list 校园招聘站，职位列表 API 公开免鉴权：
    POST https://talent.baidu.com/httservice/getPostListNew   （表单编码，非 JSON）
    body: recruitType=校招 & pageSize=100 & curPage=N & keyWord=
          （recruitType 的合法值就是中文"校招"，"校园招聘"/"college" 等都会被拒）
    resp: data.list[]（name=职位名, workPlace=地点, postId=唯一标识,
          workContent=JD）+ data.pages/total。
"""
import logging
import math
import time

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class BaiduCrawler(BaseCrawler):
    API = "https://talent.baidu.com/httservice/getPostListNew"
    DETAIL = "https://talent.baidu.com/jobs/detail/"
    PAGE_SIZE = 20  # 服务端上限 20（30+ 会报 Illegal argument）
    MAX_PAGES = 30
    JD_RAW_LIMIT = 12000
    RECRUIT_TYPE = "校招"  # 合法值是中文"校招"

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://talent.baidu.com/jobs/list",
            "Origin": "https://talent.baidu.com",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            data = {"recruitType": self.RECRUIT_TYPE, "pageSize": self.PAGE_SIZE,
                    "curPage": page, "keyWord": ""}
            result = None
            for attempt in range(self.REQUEST_ATTEMPTS + 2):
                try:
                    resp = requests.post(self.API, data=data, headers=headers, timeout=30)
                    body = resp.json()
                    if body.get("status") != "ok":
                        logger.warning("[%s] 百度 API 返回非 ok: %s", self.company_name, body.get("message"))
                        self.fetch_failed = True
                        self.pagination_termination_reason = "api_status_not_ok"
                        return jobs
                    result = body.get("data") or {}
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 百度 API 第%d页第%d次失败: %s",
                                   self.company_name, page, attempt + 1, e)
                    time.sleep(min(5.0, 1.0 * (attempt + 1)))
            if result is None:
                self.fetch_failed = True
                self.pagination_termination_reason = "page_request_failed"
                break
            self.pages_seen = page
            plist = result.get("list") or []
            total = int(result.get("total") or len(plist))
            pages = int(result.get("pages") or max(1, math.ceil(total / self.PAGE_SIZE)))
            self.advertised_total = total
            self.total_pages = pages
            if not plist:
                self.pagination_complete = page >= pages or total == 0
                self.pagination_termination_reason = (
                    "empty_terminal_page" if self.pagination_complete else "empty_page_before_total"
                )
                break
            for x in plist:
                pid = str(x.get("postId") or x.get("jobId") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                title = (x.get("name") or "").strip()
                if not title or len(title) < 2:
                    continue
                city = (x.get("workPlace") or "").replace(",", "、")[:40]
                duties = str(x.get("workContent") or "").strip()
                requirements = str(x.get("serviceCondition") or "").strip()
                jd_parts = []
                if duties:
                    jd_parts.extend(["岗位职责", duties])
                if requirements:
                    jd_parts.extend(["任职要求", requirements])
                jd_raw = "\n".join(jd_parts)[:self.JD_RAW_LIMIT]
                jobs.append(self._make_job(title=title, city=city,
                                           jd_url=f"{self.DETAIL}{pid}", jd_raw=jd_raw))
            if page >= pages:
                self.pagination_complete = len(seen) >= total
                self.pagination_termination_reason = (
                    "advertised_total_reached"
                    if self.pagination_complete
                    else "advertised_total_mismatch"
                )
                break
            time.sleep(0.3)

        if not self.pagination_complete and self.total_pages is not None:
            self.has_more = self.pages_seen < self.total_pages
            if self.pages_seen >= self.MAX_PAGES and self.has_more:
                self.pagination_termination_reason = "max_pages_reached"

        logger.info("[%s] 百度 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
