"""智明星通 2027 校招飞书入口包装。"""

from __future__ import annotations

from ..job_filters import filter_formal_campus_jobs

from .feishu import GenericFeishuCrawler


def fetch_full_job_description(job: dict) -> str:
    """Resolve the shared detail helper lazily to avoid package import cycles."""
    from ..job_details import fetch_full_job_description as fetch_detail

    return fetch_detail(job)


def is_jd_incomplete(job: dict) -> bool:
    """Resolve JD validation lazily while retaining a patchable test seam."""
    from ..job_details import is_jd_incomplete as check_incomplete

    return check_incomplete(job)


class ElexCampusCrawler(GenericFeishuCrawler):
    """在届别判断前补齐详情，防止活动页覆盖岗位正文中的 26 届冲突。"""

    EXPECTED_FORMAL_TOTAL = 10

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False

    def fetch(self) -> list[dict]:
        listed = super().fetch()
        formal, _ = filter_formal_campus_jobs(listed)
        self.detail_expected_total = len(formal)

        hydrated = []
        for job in formal:
            detail = fetch_full_job_description(job)
            hydrated.append({**job, "jd_raw": detail or job.get("jd_raw") or ""})

        # Some internship evidence exists only in the detail page.
        formal, _ = filter_formal_campus_jobs(hydrated)
        self.detail_count = sum(not is_jd_incomplete(job) for job in formal)
        self.detail_complete = (
            len(formal) == self.EXPECTED_FORMAL_TOTAL
            and self.detail_count == self.EXPECTED_FORMAL_TOTAL
        )
        return formal
