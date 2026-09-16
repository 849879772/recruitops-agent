import logging
import math
from datetime import datetime

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class JDCrawler(BaseCrawler):
    """京东校招：campus.jd.com 公开岗位分页接口。"""

    PROJECT_API = "https://campus.jd.com/api/wx/position/getProjectList"
    PAGE_API = "https://campus.jd.com/api/wx/position/page"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 12000
    # present/talent are formal campus tracks. Internship is intentionally
    # excluded because many internship titles do not contain the word 实习.
    TYPES = ("present", "talent")

    def _headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://campus.jd.com/",
            "Content-Type": "application/json",
        }

    def _plan_ids(self) -> dict[str, list[int]]:
        try:
            resp = requests.get(self.PROJECT_API, headers=self._headers(), timeout=20)
            resp.raise_for_status()
            projects = ((resp.json().get("body") or {}).get("projectList") or [])
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 京东项目接口失败: %s", self.company_name, e)
            return {}
        out: dict[str, list[int]] = {}
        for project in projects:
            code = project.get("code")
            ids = []
            for group in project.get("groupList") or []:
                for plan in group.get("planMapList") or []:
                    if plan.get("id") is not None:
                        ids.append(int(plan["id"]))
            if code and ids:
                out[code] = ids
        return out

    @staticmethod
    def _date_ms_to_day(value) -> str:
        try:
            return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return ""

    def _fetch_type(self, recruit_type: str, plan_ids: list[int] | None = None) -> list[dict]:
        jobs, seen = [], set()
        page = 0
        total_pages = 1
        while page < min(total_pages, self.MAX_PAGES):
            payload = {
                "pageSize": self.PAGE_SIZE,
                "pageIndex": page,
                "parameter": {
                    "positionName": "",
                    "planIdList": plan_ids or [],
                    "jobDirectionCodeList": [],
                    "workCityCodeList": [],
                    "positionDeptList": [],
                },
            }
            try:
                resp = requests.post(
                    f"{self.PAGE_API}?type={recruit_type}",
                    json=payload,
                    headers=self._headers(),
                    timeout=20,
                )
                resp.raise_for_status()
                body = resp.json().get("body") or {}
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 京东岗位接口失败 type=%s page=%s: %s",
                               self.company_name, recruit_type, page, e)
                break

            items = body.get("items") or []
            total = body.get("totalNumber") or len(items)
            total_pages = max(1, math.ceil(int(total) / self.PAGE_SIZE)) if str(total).isdigit() else total_pages
            for item in items:
                title = (item.get("positionName") or "").strip()
                publish_id = item.get("publishId") or item.get("reqId") or title
                key = f"{recruit_type}:{publish_id}"
                if not title or key in seen:
                    continue
                seen.add(key)
                cities = []
                for req in item.get("requirementVoList") or []:
                    city = req.get("workCity")
                    if city and city not in cities:
                        cities.append(city)
                duties = str(item.get("workContent") or "").strip()
                requirements = str(item.get("qualification") or "").strip()
                jd_raw = "\n".join(
                    x for x in ["岗位职责", duties, "任职要求", requirements] if x
                )
                jobs.append(self._make_job(
                    title=title,
                    city=" / ".join(cities)[:80],
                    # The API endpoint is only used for crawling. The public SPA route
                    # below opens the concrete job detail page in a browser.
                    jd_url=f"https://campus.jd.com/#/details?type={recruit_type}&id={publish_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                    published_at=self._date_ms_to_day(item.get("publishTime")),
                    link_kind="detail",
                ))
            if not items:
                break
            page += 1
        return jobs

    def fetch(self) -> list[dict]:
        jobs = []
        for recruit_type in self.TYPES:
            # The current public page starts from pageIndex=0 and leaves
            # planIdList empty until the user selects a sub-project. Sending
            # getProjectList IDs by default now produces an empty result.
            jobs.extend(self._fetch_type(recruit_type))
        logger.info("[%s] 京东抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
