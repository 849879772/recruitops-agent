"""掌趣科技官方 2027 秋季校园招聘 Moka 包装爬虫。

掌趣科技的 Moka 页面把正式岗位、实习岗位和一条历史残留放在同一个
岗位列表中。通用 Moka 爬虫只能拿到列表卡片，因此这里复用它的列表解析
与分页路由，再逐条渲染唯一的 ``#/job/<uuid>`` 页面补齐 JD，最后只返回
当前正式秋招岗位。
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import re
from collections import Counter
from urllib.parse import urlsplit

from .moka import MokaRecruitCrawler
from .render import render_page

logger = logging.getLogger(__name__)


def fetch_full_job_description(job: dict) -> str:
    """Resolve the shared detail helper lazily to avoid package import cycles."""
    from ..job_details import fetch_full_job_description as fetch_detail

    return fetch_detail(job)


def is_jd_incomplete(job: dict) -> bool:
    """Resolve JD validation lazily while retaining a patchable test seam."""
    from ..job_details import is_jd_incomplete as check_incomplete

    return check_incomplete(job)


class OurPalmCrawler(MokaRecruitCrawler):
    """抓取掌趣科技 ``ourpalm/43628`` 项目的正式 2027 校招岗位。"""

    OFFICIAL_HOST = "app.mokahr.com"
    PROJECT_PATH = "/campus-recruitment/ourpalm/43628"
    CAMPAIGN_TEXT = "掌趣科技2027届秋季校园招聘正式启动"
    CAMPAIGN_SOURCE = "掌趣科技官方招聘公告"
    DETAIL_WORKERS = max(1, int(os.environ.get("OURPALM_DETAIL_WORKERS", "3")))

    _DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
    _INTERNSHIP_RE = re.compile(r"实习|intern(?:ship)?", re.IGNORECASE)
    _EARLY_BATCH_RE = re.compile(r"提前批|提前招聘|提前选拔|early\s*batch", re.IGNORECASE)

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.expected_total: int | None = None
        self.expected_pages: int | None = None
        self.pages_fetched = 0
        self.page_sizes: list[int] = []
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.detail_expected_total = 0
        self.detail_success_count = 0
        self.detail_unique_urls = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []

        self.excluded_records: list[dict[str, str]] = []
        self.filtered_internship_count = 0
        self.filtered_historical_count = 0
        self.filtered_early_batch_count = 0
        self.filtered_special_program_count = 0
        self.retained_count = 0
        self.metrics: dict[str, object] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        reason_counts = Counter(
            item.get("reason", "unknown") for item in self.excluded_records
        )
        self.metrics = {
            "campaign_text": self.CAMPAIGN_TEXT,
            "campaign_source": self.CAMPAIGN_SOURCE,
            "pagination_complete": self.pagination_complete,
            "pagination_expected_total": self.expected_total,
            "pagination_expected_pages": self.expected_pages,
            "pagination_pages_fetched": self.pages_fetched,
            "pagination_page_sizes": list(self.page_sizes),
            "pagination_raw_listed_count": self.raw_listed_count,
            "pagination_unique_listed_count": self.unique_listed_count,
            "pagination_termination_reason": self.pagination_termination_reason,
            "detail_expected_total": self.detail_expected_total,
            "detail_success_count": self.detail_success_count,
            "detail_unique_urls": self.detail_unique_urls,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
            "retained_count": self.retained_count,
            "excluded_count": len(self.excluded_records),
            "excluded_reason_counts": dict(reason_counts),
            "filtered_internship_count": self.filtered_internship_count,
            "filtered_historical_count": self.filtered_historical_count,
            "filtered_early_batch_count": self.filtered_early_batch_count,
            "filtered_special_program_count": self.filtered_special_program_count,
            "pagination": {
                "expected_total": self.expected_total,
                "expected_pages": self.expected_pages,
                "pages_fetched": self.pages_fetched,
                "page_sizes": list(self.page_sizes),
                "raw_listed_count": self.raw_listed_count,
                "unique_listed_count": self.unique_listed_count,
                "complete": self.pagination_complete,
                "termination_reason": self.pagination_termination_reason,
            },
            "detail": {
                "expected_total": self.detail_expected_total,
                "success_count": self.detail_success_count,
                "unique_urls": self.detail_unique_urls,
                "complete": self.detail_complete,
                "failures": list(self.detail_failures),
            },
            "excluded_records": list(self.excluded_records),
        }

    @classmethod
    def _is_target_url(cls, url: str) -> bool:
        parsed = urlsplit(url or "")
        return (
            parsed.netloc.casefold() == cls.OFFICIAL_HOST
            and parsed.path.rstrip("/").casefold() == cls.PROJECT_PATH
        )

    @classmethod
    def _published_at(cls, text: str) -> str:
        match = cls._DATE_RE.search(text or "")
        return match.group(1) if match else ""

    @classmethod
    def _is_internship(cls, title: str, employment_type: str, raw: str) -> bool:
        """Use list/API metadata or title, never generic JD experience text."""
        return bool(
            cls._INTERNSHIP_RE.search(title or "")
            or cls._INTERNSHIP_RE.search(employment_type or "")
        )

    def _make_job(
        self,
        title: str,
        city: str = "",
        job_type: str = "校招",
        jd_url: str = "",
        jd_raw: str = "",
        published_at: str = "",
        link_kind: str = "detail",
        campaign_text: str = "",
    ) -> dict:
        """Normalize Moka's employment-type metadata before detail hydration."""
        employment_type = str(job_type or "").strip()
        if employment_type not in {"全职", "实习", "兼职"}:
            employment_type = str(city or "").strip()
        internship = self._is_internship(title, employment_type, jd_raw)
        job = super()._make_job(
            title=title,
            city=city if str(city or "").strip() not in {"全职", "实习", "兼职"} else "",
            job_type="实习" if internship else "校招",
            jd_url=jd_url,
            jd_raw=jd_raw,
            published_at=published_at or self._published_at(jd_raw),
            link_kind=link_kind,
            campaign_text=campaign_text or self.CAMPAIGN_TEXT,
        )
        job["employment_type"] = employment_type
        job["source_job_id"] = jd_url.rsplit("/", 1)[-1]
        return job

    @classmethod
    def _exclusion_reason(cls, job: dict) -> str | None:
        title = str(job.get("title") or "")
        employment_type = str(job.get("employment_type") or "")
        raw = str(job.get("jd_raw") or "")
        if cls._is_internship(title, employment_type, raw):
            return "internship_api_or_title"
        if cls._EARLY_BATCH_RE.search(" ".join(
            str(job.get(field) or "")
            for field in ("title", "job_type", "jd_raw", "jd_url")
        )):
            return "early_batch"
        if str(job.get("published_at") or "").startswith("2025-"):
            return "historical_published_2025"
        if "暑期训练营" in title:
            return "summer_training_camp_non_formal"
        return None

    def _record_exclusion(self, job: dict, reason: str) -> None:
        self.excluded_records.append(
            {
                "title": str(job.get("title") or ""),
                "url": str(job.get("jd_url") or ""),
                "published_at": str(job.get("published_at") or ""),
                "reason": reason,
            }
        )
        if reason == "internship_api_or_title":
            self.filtered_internship_count += 1
        elif reason == "historical_published_2025":
            self.filtered_historical_count += 1
        elif reason == "early_batch":
            self.filtered_early_batch_count += 1
        elif reason == "summer_training_camp_non_formal":
            self.filtered_special_program_count += 1

    @staticmethod
    def _hydrate_one(job: dict) -> tuple[dict, str | None]:
        try:
            # Force a detail request even when a future Moka card happens to
            # contain a long summary; list text is not accepted as the JD.
            detail = fetch_full_job_description({**job, "jd_raw": ""})
        except Exception as exc:  # noqa: BLE001
            return job, f"detail_fetch_error:{type(exc).__name__}"
        if not detail or is_jd_incomplete({**job, "jd_raw": detail}):
            return job, "detail_jd_incomplete"
        return {**job, "jd_raw": detail}, None

    def _hydrate_and_filter(self, listed_jobs: list[dict]) -> list[dict]:
        self.detail_expected_total = len(listed_jobs)
        self.detail_unique_urls = len({job.get("jd_url") for job in listed_jobs})

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.DETAIL_WORKERS
        ) as executor:
            hydrated = list(executor.map(self._hydrate_one, listed_jobs))

        retained = []
        for original, failure in hydrated:
            if failure:
                self.detail_failures.append(
                    {
                        "title": str(original.get("title") or ""),
                        "url": str(original.get("jd_url") or ""),
                        "reason": failure,
                    }
                )
                self._record_exclusion(original, "detail_jd_incomplete")
                continue

            self.detail_success_count += 1
            reason = self._exclusion_reason(original)
            if reason:
                self._record_exclusion(original, reason)
                continue

            job = dict(original)
            job.update(
                {
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "cohort_source": self.CAMPAIGN_SOURCE,
                    "cohort_evidence": self.CAMPAIGN_TEXT,
                    "campaign_text": self.CAMPAIGN_TEXT,
                    "recruitment_track": "formal",
                }
            )
            retained.append(job)

        self.detail_complete = (
            self.detail_expected_total == self.detail_success_count
            and self.detail_unique_urls == self.detail_expected_total
            and not self.detail_failures
        )
        self.retained_count = len(retained)
        self._update_metrics()
        return retained

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._is_target_url(self.careers_url):
            self.pagination_termination_reason = "wrong_official_project"
            self._update_metrics()
            return []

        listed_jobs: list[dict] = []
        seen: set[str] = set()
        for page in range(1, self.MAX_PAGES + 1):
            html = render_page(
                self._jobs_url(page),
                wait_for=None,
                timeout_ms=45000,
                extra_wait_ms=self.EXTRA_WAIT_MS,
                scroll_times=self.SCROLL_TIMES,
            )
            if not html:
                self.pages_fetched = page
                self.pagination_termination_reason = f"render_failed_page_{page}"
                self._update_metrics()
                return []

            self.pages_fetched = page
            if self.expected_total is None:
                self.expected_total = self._result_count(html)
                if self.expected_total is not None:
                    self.expected_pages = math.ceil(
                        self.expected_total / self.PAGE_SIZE
                    )
            page_jobs = self._parse_page(html, seen)
            self.page_sizes.append(len(page_jobs))
            self.raw_listed_count += len(page_jobs)
            listed_jobs.extend(page_jobs)
            self.unique_listed_count = len(seen)

            if not page_jobs:
                if self.expected_total is None:
                    self.pagination_complete = False
                    self.pagination_termination_reason = "empty_page_without_total"
                elif self.unique_listed_count < self.expected_total:
                    self.pagination_complete = False
                    self.pagination_termination_reason = f"empty_page_{page}"
                else:
                    self.pagination_complete = True
                    self.pagination_termination_reason = "empty_page"
                break

            if self.expected_total is not None and (
                self.unique_listed_count >= self.expected_total
            ):
                self.pagination_complete = (
                    self.unique_listed_count == self.expected_total
                )
                self.pagination_termination_reason = "expected_total_reached"
                break

            if self.expected_total is None and len(page_jobs) < self.PAGE_SIZE:
                self.pagination_complete = True
                self.pagination_termination_reason = "short_page"
                break

            if self.expected_pages is not None and page >= self.expected_pages:
                self.pagination_complete = (
                    self.unique_listed_count == self.expected_total
                )
                self.pagination_termination_reason = "expected_pages_reached"
                break
        else:
            self.pagination_complete = False
            self.pagination_termination_reason = "max_pages"

        self.listed_count = len(listed_jobs)
        self._update_metrics()
        if not self.pagination_complete:
            return []

        return self._hydrate_and_filter(listed_jobs)


__all__ = ["OurPalmCrawler"]
