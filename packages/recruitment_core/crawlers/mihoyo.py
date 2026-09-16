import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class MihoyoCrawler(BaseCrawler):
    """米哈游校招：ats.openout.mihoyo.com 公开岗位列表接口。"""

    API = "https://ats.openout.mihoyo.com/ats-portal/v1/job/list"
    DETAIL_API = "https://ats.openout.mihoyo.com/ats-portal/v1/job/info"
    PAGE_SIZE = 100
    MAX_PAGES = 5
    JD_RAW_LIMIT = 12000
    DETAIL_WORKERS = 20

    @staticmethod
    def _post_json(url: str, *, payload: dict, headers: dict[str, str]) -> dict:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=20)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("Mihoyo API returned a non-object payload")
                return body
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Mihoyo API failed after retries: {last_error}")

    def _fetch_detail(self, job_id: object, headers: dict[str, str]) -> dict:
        payload = self._post_json(
            self.DETAIL_API,
            payload={"id": job_id, "channelDetailIds": [1]},
            headers=headers,
        )
        if int(payload.get("code") or 0) != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError(str(payload.get("message") or "Mihoyo detail API returned no data"))
        return dict(payload["data"])

    def fetch(self) -> list[dict]:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        self.detail_failures = 0
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://jobs.mihoyo.com/#/campus",
            "Content-Type": "application/json",
            "Release-Tag": "v26.7.2-260706",
            "Accept-Language": "zh-CN",
        }
        jobs, seen = [], set()
        detail_targets: list[tuple[dict, object]] = []
        page, total_pages = 1, 1
        while page <= min(total_pages, self.MAX_PAGES):
            payload = {
                "pageNo": page,
                "pageSize": self.PAGE_SIZE,
                "channelDetailIds": [1],
                "hireType": 1,
            }
            try:
                body = self._post_json(self.API, payload=payload, headers=headers)
                data = body.get("data") or {}
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 米哈游岗位接口失败 page=%s: %s", self.company_name, page, e)
                self.fetch_failed = True
                self.pagination_termination_reason = "page_request_failed"
                break
            items = data.get("list") or []
            total = int(data.get("total") or len(items))
            total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
            self.pages_seen = page
            self.total_pages = total_pages
            self.advertised_total = total
            for item in items:
                job_id = item.get("id") or item.get("title")
                title = (item.get("title") or "").strip()
                if not title or job_id in seen:
                    continue
                seen.add(job_id)
                cities = [x.get("addressDetail") for x in item.get("addressDetailList") or [] if x.get("addressDetail")]
                jd_raw = " | ".join(
                    str(x) for x in [item.get("competencyType"), item.get("jobNature"),
                                     item.get("projectName"), item.get("jobSummary")] if x
                )
                job = self._make_job(
                    title=title,
                    city=" / ".join(cities)[:80],
                    jd_url=f"https://jobs.mihoyo.com/#/campus/position/{job_id}",
                    jd_raw=jd_raw[: self.JD_RAW_LIMIT],
                )
                jobs.append(job)
                detail_targets.append((job, job_id))
            if not items:
                self.pagination_complete = page >= total_pages or total == 0
                self.pagination_termination_reason = (
                    "empty_terminal_page" if self.pagination_complete else "empty_page_before_total"
                )
                break
            if page >= total_pages:
                self.pagination_complete = len(seen) >= total
                self.pagination_termination_reason = (
                    "advertised_total_reached"
                    if self.pagination_complete
                    else "advertised_total_mismatch"
                )
                break
            page += 1
        if not self.pagination_complete and self.total_pages is not None:
            self.has_more = self.pages_seen < self.total_pages
            if self.pages_seen >= self.MAX_PAGES and self.has_more:
                self.pagination_termination_reason = "max_pages_reached"

        if detail_targets:
            with ThreadPoolExecutor(max_workers=min(self.DETAIL_WORKERS, len(detail_targets))) as pool:
                futures = {
                    pool.submit(self._fetch_detail, job_id, headers): job
                    for job, job_id in detail_targets
                }
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        detail = future.result()
                    except Exception as exc:  # noqa: BLE001
                        self.detail_failures += 1
                        logger.debug("[%s] 米哈游岗位详情失败: %s", self.company_name, exc)
                        continue
                    parts = [
                        ("岗位职责", detail.get("description")),
                        ("任职要求", detail.get("jobRequire")),
                        ("附加说明", detail.get("addition")),
                    ]
                    job["jd_raw"] = "\n".join(
                        f"{label}\n{str(value).strip()}"
                        for label, value in parts
                        if str(value or "").strip()
                    )[: self.JD_RAW_LIMIT]
            if self.detail_failures:
                self.pagination_termination_reason += f";detail_failures={self.detail_failures}"
        logger.info("[%s] 米哈游抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
