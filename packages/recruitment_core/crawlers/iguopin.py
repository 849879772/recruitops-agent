"""Crawler for branded campus sites hosted by IGuopin."""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_API_ROOT = "https://gp-api.iguopin.com/api"
_DETAIL_PAGE = "https://www.iguopin.com/job/detail?id={job_id}"
_CAMPUS_NATURES = ["115xW5oQ", "11bTac9", "114Fay7E"]
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    ),
}


class IGuopinCrawler(BaseCrawler):
    PAGE_SIZE = 100
    MAX_PAGES = 20

    def _request_json(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs,
    ):
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = session.request(method, url, timeout=25, **kwargs)
                response.raise_for_status()
                payload = response.json()
                if int(payload.get("code") or 0) == 200:
                    return payload.get("data")
                raise RuntimeError(payload.get("msg") or "IGuopin API returned an error")
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning("[%s] 国聘接口请求失败 %s: %s", self.company_name, url, exc)
                    return None
                time.sleep(0.5 * attempt)
        return None

    def _site_config(self, session: requests.Session) -> tuple[str, list[str]]:
        host = urlparse(self.careers_url).netloc.casefold()
        domain = host.split(".", 1)[0]
        data = self._request_json(
            session,
            "GET",
            f"{_API_ROOT}/activity/exclusive/v1/info",
            params={"domain": domain},
        )
        if not isinstance(data, dict):
            return "", list(_CAMPUS_NATURES)

        company_id = str(data.get("company_id") or "").strip()
        natures = list(_CAMPUS_NATURES)
        try:
            content = json.loads(data.get("content") or "{}")
            for nav in (content.get("params") or {}).get("nav") or []:
                if nav.get("route") == "/job-campus":
                    configured = str((nav.get("props") or {}).get("nature") or "")
                    parsed = [value.strip() for value in configured.split(",") if value.strip()]
                    if parsed:
                        natures = parsed
                    break
        except (TypeError, ValueError):
            pass
        return company_id, natures

    @staticmethod
    def _city(row: dict) -> str:
        districts = row.get("district_list") or []
        cities = []
        for district in districts:
            value = str((district or {}).get("area_cn") or "").strip()
            if value and value not in cities:
                cities.append(value)
        return "、".join(cities)

    def fetch(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(_HEADERS)
        parsed = urlparse(self.careers_url)
        site_root = f"{parsed.scheme}://{parsed.netloc}"
        session.headers.update({"Origin": site_root, "Referer": f"{site_root}/"})
        company_id, natures = self._site_config(session)
        if not company_id:
            return []

        rows = []
        total = None
        for page in range(1, self.MAX_PAGES + 1):
            data = self._request_json(
                session,
                "POST",
                f"{_API_ROOT}/jobs/v1/list",
                json={
                    "page": page,
                    "page_size": self.PAGE_SIZE,
                    "source": "s_job_list",
                    "nature": natures,
                    "company_id_with_sub": company_id,
                },
            )
            if not isinstance(data, dict):
                break
            page_rows = data.get("list") or []
            rows.extend(row for row in page_rows if isinstance(row, dict))
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = None
            if not page_rows or (total is not None and len(rows) >= total):
                break

        jobs = []
        seen_ids = set()
        for row in rows:
            job_id = str(row.get("job_id") or "").strip()
            title = str(row.get("job_name") or "").strip()
            if not job_id or not title or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            raw = str(row.get("contents") or "").strip()
            if raw and not raw.startswith(("职位描述", "岗位职责")):
                raw = f"职位描述\n{raw}"
            jobs.append(self._make_job(
                title=title,
                city=self._city(row),
                job_type=" ".join(
                    part for part in (
                        "校园招聘",
                        str(row.get("nature_cn") or "").strip(),
                        str(row.get("recruitment_type_cn") or "").strip(),
                    )
                    if part
                ),
                jd_url=_DETAIL_PAGE.format(job_id=job_id),
                jd_raw=raw[:12000],
                published_at=str(row.get("start_time") or "")[:10],
                link_kind="detail",
            ))

        logger.info("[%s] 国聘官方 API 抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
