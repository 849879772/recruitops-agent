"""BOE campus crawler for the Beisen 2022 portal API."""
import logging

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class BOECrawler(BaseCrawler):
    API = "https://campus.boe.com/api/Jobad/GetJobAdPageList"
    PAGE_SIZE = 50
    MAX_PAGES = 20
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": "https://campus.boe.com/campus/jobs",
            "User-Agent": "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
        }
        jobs, seen = [], set()
        for page in range(1, self.MAX_PAGES + 1):
            payload = {"pageIndex": page, "pageSize": self.PAGE_SIZE}
            try:
                resp = requests.post(self.API, json=payload, headers=headers, timeout=30)
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] BOE API page %d failed: %s", self.company_name, page, exc)
                break

            rows = data.get("Data") or []
            if not rows:
                break
            for item in rows:
                jid = str(item.get("JobAdId") or item.get("Id") or "")
                title = item.get("JobAdName") or ""
                if not title or jid in seen:
                    continue
                seen.add(jid)
                city = "、".join(item.get("LocNames") or [])[:40]
                duties = str(item.get("Duty") or "").strip()
                requirements = str(item.get("Require") or "").strip()
                jd_raw = "\n".join(
                    x for x in ["岗位职责", duties, "任职要求", requirements] if x
                )[: self.JD_RAW_LIMIT]
                jobs.append(
                    self._make_job(
                        title=title,
                        city=city,
                        jd_url=f"https://campus.boe.com/campus/jobs?jobAdId={jid}",
                        jd_raw=jd_raw,
                        published_at=(item.get("ChangeDate") or "")[:10],
                    )
                )
            if len(jobs) >= int(data.get("Count") or 0):
                break

        logger.info("[%s] BOE caught %d jobs", self.company_name, len(jobs))
        return jobs
