"""TP-LINK (Pu Lian), not TP-Link Global: public campus home-data API."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from .base import BaseCrawler, launch_browser


HOST = "hr.tp-link.com.cn"
HOME = f"https://{HOST}/"
API_URL = f"{HOME}api/v1/home/homedata"


class TPLinkCrawler(BaseCrawler):
    """Consume the same unpaginated payload as html/js/home.min.js."""

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        parsed = urlsplit(careers_url)
        if (parsed.scheme != "https" or parsed.netloc != HOST
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("TP-LINK adapter requires its exact public campus home")
        self._reset()

    def _reset(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.total_pages = None
        self.advertised_total = None
        self.has_more = False
        self.fetch_failed = False
        self.crawl_error_code = ""
        self.failure_reason = ""
        self.pagination_diagnostics = []
        self.resolved_source_url = HOME
        self.pagination_evidence = []

    @staticmethod
    def _clean_jd_field(value: object) -> str:
        """Reuse the shared API-text cleaner without importing hydration at module load."""
        from ..job_details import _clean_api_text

        return _clean_api_text(value)

    @staticmethod
    def parse_payload(payload: dict) -> list[dict]:
        if (not isinstance(payload, dict)
                or not all(isinstance(payload.get(key), list)
                           for key in ("jobs", "jobClasses", "workPlaces"))):
            raise ValueError("invalid_homedata_schema")
        # Never silently accept a future paginated variant as the complete home.
        if any(key in payload for key in ("total", "totalCount", "page", "hasMore", "next")):
            raise ValueError("unexpected_pagination_contract")
        unique = {}
        for row in payload["jobs"]:
            if not isinstance(row, dict):
                raise ValueError("invalid_job_record")
            native_id = row.get("Id")
            title = row.get("JobName")
            if (type(native_id) is not int or native_id <= 0
                    or not isinstance(title, str) or not title.strip()
                    or "{{" in title or "ClassId" not in row):
                raise ValueError("invalid_job_identity")
            for field in ("Duty", "Requirement", "JobAddress", "Batch", "Education"):
                if row.get(field) is not None and not isinstance(row[field], str):
                    raise ValueError(f"invalid_job_field:{field}")
            if native_id in unique and unique[native_id] != row:
                raise ValueError("conflicting_job_identity")
            unique[native_id] = row
        return list(unique.values())

    def _consume(self, payload: dict, campaign_text: str) -> list[dict]:
        rows = self.parse_payload(payload)
        jobs = []
        for row in rows:
            raw_duty = row.get("Duty") or ""
            raw_requirement = row.get("Requirement") or ""
            duty = self._clean_jd_field(raw_duty)
            requirement = self._clean_jd_field(raw_requirement)
            sections = [text for text in (duty, requirement) if text.strip()]
            job = self._make_job(
                row["JobName"].strip(), city=row.get("JobAddress") or "",
                jd_url=f"{HOME}jobDetail/{row['Id']}",
                jd_raw="\n\n".join(sections), campaign_text=campaign_text,
            )
            job.update(
                source_job_id=str(row["Id"]), source_url=HOME,
                raw_duty=raw_duty, raw_requirement=raw_requirement,
                raw_batch=row.get("Batch") or "", education=row.get("Education") or "",
                source_api_url=API_URL,
                jd_raw_complete=bool(duty.strip() not in {"", "--", "-"}
                                     and requirement.strip() not in {"", "--", "-"}),
            )
            jobs.append(job)
        self.pages_seen = 1
        # This is an unpaginated resource. No official page/record total exists.
        self.pagination_complete = True
        self.pagination_termination_reason = "unpaginated_homedata_array_consumed"
        self.pagination_evidence = [{
            "source_url": API_URL, "pagination_mode": "unpaginated",
            "response_job_count": len(payload["jobs"]), "unique_job_count": len(jobs),
            "advertised_total": None, "total_pages": None,
            "termination": self.pagination_termination_reason,
            "contract_evidence": "html/js/home.min.js:getJobInfo/created",
            "payload_sha256": hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        }]
        return jobs

    def fetch(self) -> list[dict]:
        from playwright.sync_api import Error, sync_playwright

        self._reset()
        try:
            with sync_playwright() as pw:
                browser = launch_browser(pw, headless=True)
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 900})
                    try:
                        page = context.new_page()
                        response = page.goto(HOME, wait_until="domcontentloaded", timeout=25000)
                        if urlsplit(page.url).netloc != HOST:
                            raise ValueError("foreign_redirect")
                        if response is None or response.status != 200:
                            raise ValueError(f"home_http_{response.status if response else 'missing'}")
                        # Hidden login widgets do not gate this public read-only API.
                        heading = page.get_by_text("2027校园招聘", exact=True)
                        heading.wait_for(state="visible", timeout=10000)
                        result = page.evaluate("""async url => {
                            const controller = new AbortController();
                            const timer = setTimeout(() => controller.abort(), 20000);
                            try {
                                const r = await fetch(url, {credentials: 'omit', redirect: 'error',
                                    signal: controller.signal});
                                return {status: r.status, text: await r.text()};
                            } finally { clearTimeout(timer); }
                        }""", API_URL)
                        if result["status"] != 200:
                            raise ValueError(f"homedata_http_{result['status']}")
                        return self._consume(json.loads(result["text"]), heading.inner_text())
                    finally:
                        context.close()
                finally:
                    browser.close()
        except (Error, ValueError) as exc:
            self.fetch_failed = True
            self.crawl_error_code = "tplink_fetch_failed"
            self.pagination_termination_reason = self.crawl_error_code
            self.failure_reason = f"{type(exc).__name__}: {exc}"
            self.pagination_diagnostics = [{"reason": self.failure_reason}]
            return []
