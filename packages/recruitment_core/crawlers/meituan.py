"""美团校招爬虫 —— 自建站 zhaopin.meituan.com，走公开 JSON API。

campus.meituan.com 跳转到 zhaopin.meituan.com/web/campus（校园招聘官网）。
其职位列表 API 公开免鉴权：
    POST https://zhaopin.meituan.com/api/official/job/getJobList
    body: {"page":{"pageNo":N, "pageSize":100},
           "jobType":[{"code":"1", "subCode":[]}], ...}
    resp: data.list[]（name=职位名, cityList[].name=地点, jobUnionId=唯一标识,
          jobDuty/jobRequirement=JD）+ data.page.totalPage/totalCount。
官网把正式校招和实习分别编码为 jobType=1/2；这里只请求正式校招。
"""
import logging
import time

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class MeituanCrawler(BaseCrawler):
    API = "https://zhaopin.meituan.com/api/official/job/getJobList"
    PAGE_SIZE = 100
    MAX_PAGES = 10
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Referer": "https://zhaopin.meituan.com/web/campus",
            "Origin": "https://zhaopin.meituan.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            body = {
                "page": {"pageNo": page, "pageSize": self.PAGE_SIZE},
                "jobShareType": "1",
                "keywords": "",
                "cityList": [],
                "department": [],
                "jfJgList": [],
                "jobType": [{"code": "1", "subCode": []}],
                "typeCode": [],
                "specialCode": [],
            }
            data = None
            for attempt in range(3):  # 大站偶发限流/SSL 中断，轻量重试
                try:
                    resp = requests.post(self.API, json=body, headers=headers, timeout=30)
                    data = resp.json().get("data") or {}
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 美团 API 第%d页第%d次失败: %s",
                                   self.company_name, page, attempt + 1, e)
                    time.sleep(1.5 * (attempt + 1))
            if data is None:
                break  # 三次仍失败，止损返回已抓到的
            plist = data.get("list") or []
            if not plist:
                break
            for x in plist:
                if str(x.get("jobType") or "") != "1":
                    continue
                jid = str(x.get("jobUnionId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                title = (x.get("name") or "").strip()
                if not title or len(title) < 2:
                    continue
                city = "、".join(c.get("name", "") for c in (x.get("cityList") or []) if c.get("name"))[:40]
                duties = str(x.get("jobDuty") or x.get("desc") or "").strip()
                requirements = str(x.get("jobRequirement") or "").strip()
                jd_raw = "\n".join(
                    part for part in ["岗位职责", duties, "任职要求", requirements] if part
                )[:self.JD_RAW_LIMIT]
                jd_url = (
                    "https://zhaopin.meituan.com/web/position/detail"
                    f"?jobUnionId={jid}&highlightType=campus"
                )
                jobs.append(self._make_job(title=title, city=city, jd_url=jd_url, jd_raw=jd_raw))
            page_info = data.get("page") or {}
            if page >= (page_info.get("totalPage") or 0):
                break
            time.sleep(0.3)  # 页间小延时，降低限流概率

        logger.info("[%s] 美团 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
