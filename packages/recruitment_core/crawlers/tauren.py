"""Deterministic crawler for Tauren Semiconductor's official Liepin ATS page."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class TaurenCrawler(BaseCrawler):
    """Parse the job array embedded in the official 2027 campus bundle.

    The page is a static Vue shell.  Its current bundle contains a
    ``JSON.parse(`[...]`)`` payload, so there is no need to render the page or
    follow UI actions.  The crawler fails closed when the campaign marker,
    source count, direct job links, or JD sections do not match expectations.
    """

    OFFICIAL_HOST = "xy.liepin.com"
    PROJECT_PATH = "/taurentech"
    CAMPAIGN_TEXT = "韬润半导体2027届秋季校园招聘"
    EXPECTED_SOURCE_TOTAL = 10
    EXPECTED_RETAINED_TOTAL = 9
    JD_RAW_LIMIT = 12000
    _EMBEDDED_JSON_RE = re.compile(r"JSON\.parse\(`(?P<payload>(?:\\.|[^`])*)`\)")
    _INTERNSHIP_RE = re.compile(r"实习|实习生|intern(?:ship)?", re.IGNORECASE)
    _JOB_ID_RE = re.compile(r"^/lptjob/(\d+)$", re.IGNORECASE)

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.campaign_validated = False
        self.bundle_url = ""
        self.expected_total = self.EXPECTED_SOURCE_TOTAL
        self.expected_pages = 1
        self.pages_fetched = 0
        self.page_sizes: list[int] = []
        self.raw_listed_count = 0
        self.unique_listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"

        self.source_detail_count = 0
        self.source_detail_unique_urls = 0
        self.detail_expected_total = self.EXPECTED_RETAINED_TOTAL
        self.detail_count = 0
        self.detail_unique_urls = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []

        self.excluded_records: list[dict[str, str]] = []
        self.filtered_internship_count = 0
        self.retained_count = 0
        self.metrics: dict[str, object] = {}
        self._update_metrics()

    def _update_metrics(self) -> None:
        self.metrics = {
            "campaign_text": self.CAMPAIGN_TEXT,
            "campaign_validated": self.campaign_validated,
            "bundle_url": self.bundle_url,
            "expected_total": self.expected_total,
            "expected_pages": self.expected_pages,
            "pages_fetched": self.pages_fetched,
            "page_sizes": list(self.page_sizes),
            "raw_listed_count": self.raw_listed_count,
            "unique_listed_count": self.unique_listed_count,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "source_detail_count": self.source_detail_count,
            "source_detail_unique_urls": self.source_detail_unique_urls,
            "detail_expected_total": self.detail_expected_total,
            "detail_count": self.detail_count,
            "detail_unique_urls": self.detail_unique_urls,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
            "filtered_internship_count": self.filtered_internship_count,
            "excluded_count": len(self.excluded_records),
            "retained_count": self.retained_count,
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
                "source_count": self.source_detail_count,
                "source_unique_urls": self.source_detail_unique_urls,
                "expected_total": self.detail_expected_total,
                "count": self.detail_count,
                "unique_urls": self.detail_unique_urls,
                "complete": self.detail_complete,
                "failures": list(self.detail_failures),
            },
            "excluded_records": list(self.excluded_records),
        }

    def pagination_metrics(self) -> dict[str, object]:
        return dict(self.metrics["pagination"])

    def detail_metrics(self) -> dict[str, object]:
        return dict(self.metrics["detail"])

    @classmethod
    def _is_target_url(cls, url: str) -> bool:
        parsed = urlsplit(url or "")
        return (
            parsed.netloc.casefold() == cls.OFFICIAL_HOST
            and parsed.path.rstrip("/").casefold() == cls.PROJECT_PATH
        )

    @staticmethod
    def _clean_text(value: object) -> str:
        text = str(value or "").replace("\\r\\n", "\n")
        text = text.replace("\\n", "\n").replace("\\r", "\n")
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _response_text(response: object) -> str:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", errors="replace")
        return str(getattr(response, "text", "") or "")

    @classmethod
    def _direct_job_id(cls, url: object) -> str:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() != "www.liepin.com":
            return ""
        match = cls._JOB_ID_RE.fullmatch(parsed.path.rstrip("/"))
        return match.group(1) if match else ""

    @classmethod
    def _extract_job_payload(cls, bundle: str) -> list[dict[str, object]] | None:
        candidates: list[list[dict[str, object]]] = []
        for match in cls._EMBEDDED_JSON_RE.finditer(bundle):
            try:
                payload = json.loads(match.group("payload"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                continue
            if all(item.get("title") and item.get("applyUrl") for item in payload):
                candidates.append(payload)
        if not candidates:
            return None
        exact = [items for items in candidates if len(items) == cls.EXPECTED_SOURCE_TOTAL]
        return exact[0] if exact else candidates[0]

    def _load_bundle(self, page_html: str) -> tuple[str, str] | None:
        soup = BeautifulSoup(page_html, "html.parser")
        page_evidence = " ".join(
            part for part in (
                soup.title.get_text(" ", strip=True) if soup.title else "",
                soup.get_text(" ", strip=True),
            )
            if part
        )
        if self.CAMPAIGN_TEXT not in page_evidence:
            self.pagination_termination_reason = "campaign_evidence_missing"
            return None
        self.campaign_validated = True

        scripts = [
            urljoin(self.careers_url, str(node.get("src") or "").strip())
            for node in soup.find_all("script", src=True)
        ]
        scripts = [url for url in scripts if urlsplit(url).netloc.casefold() == self.OFFICIAL_HOST]
        if not scripts:
            self.pagination_termination_reason = "bundle_script_missing"
            return None

        for bundle_url in scripts:
            response = self._get(bundle_url, timeout=30)
            if response is None:
                continue
            payload = self._extract_job_payload(self._response_text(response))
            if payload is not None:
                self.bundle_url = bundle_url
                return bundle_url, self._response_text(response)
        self.pagination_termination_reason = "embedded_jobs_missing"
        return None

    def _build_jobs(self, payload: list[dict[str, object]]) -> list[dict[str, object]]:
        self.raw_listed_count = len(payload)
        self.source_detail_count = len(payload)
        source_urls: set[str] = set()
        source_ids: set[str] = set()
        listed_identities: set[str] = set()
        listed: list[dict[str, object]] = []

        for item in payload:
            title = self._clean_text(item.get("title"))
            description = self._clean_text(item.get("description"))
            requirements = self._clean_text(item.get("requirements"))
            apply_url = str(item.get("applyUrl") or "").strip()
            listed_identities.add(apply_url or title)
            source_id = self._direct_job_id(apply_url)
            if not title or not description or not requirements or not source_id:
                self.detail_failures.append(
                    {
                        "title": title,
                        "url": apply_url,
                        "reason": "invalid_direct_link_or_incomplete_jd",
                    }
                )
                continue
            if source_id in source_ids:
                self.detail_failures.append(
                    {"title": title, "url": apply_url, "reason": "duplicate_detail"}
                )
                continue

            source_ids.add(source_id)
            source_urls.add(apply_url)
            job = self._make_job(
                title=title,
                city=self._clean_text(item.get("location")),
                job_type="校招",
                jd_url=apply_url,
                jd_raw=f"职位描述\n{description}\n任职要求\n{requirements}",
                link_kind="detail",
                campaign_text=self.CAMPAIGN_TEXT,
            )
            job.update(
                {
                    "source_job_id": source_id,
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "cohort_source": "韬润半导体官方猎聘 ATS",
                    "cohort_evidence": self.CAMPAIGN_TEXT,
                    "recruitment_track": "formal",
                }
            )
            listed.append(job)

        self.unique_listed_count = len(listed_identities)
        self.source_detail_unique_urls = len(source_urls)
        self.detail_unique_urls = len(source_urls)
        self.detail_expected_total = self.EXPECTED_RETAINED_TOTAL

        retained: list[dict[str, object]] = []
        for job in listed:
            title = str(job.get("title") or "")
            if self._INTERNSHIP_RE.search(title):
                self.filtered_internship_count += 1
                self.excluded_records.append(
                    {
                        "title": title,
                        "url": str(job.get("jd_url") or ""),
                        "reason": "internship_title",
                    }
                )
                continue
            retained.append(job)

        self.detail_count = len(retained)
        self.detail_unique_urls = len({str(job.get("jd_url") or "") for job in retained})
        self.retained_count = len(retained)
        self.detail_complete = (
            not self.detail_failures
            and len(retained) == self.EXPECTED_RETAINED_TOTAL
            and self.detail_unique_urls == self.EXPECTED_RETAINED_TOTAL
            and all(
                "职位描述\n" in str(job.get("jd_raw") or "")
                and "\n任职要求\n" in str(job.get("jd_raw") or "")
                for job in retained
            )
        )
        return retained

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._is_target_url(self.careers_url):
            self.pagination_termination_reason = "invalid_official_project_url"
            self._update_metrics()
            return []

        response = self._get(self.careers_url, timeout=30)
        if response is None:
            self.pagination_termination_reason = "request_failed"
            self._update_metrics()
            return []

        loaded = self._load_bundle(self._response_text(response))
        self.pages_fetched = 1
        if loaded is None:
            self._update_metrics()
            return []

        _, bundle = loaded
        payload = self._extract_job_payload(bundle)
        if payload is None or len(payload) != self.EXPECTED_SOURCE_TOTAL:
            self.pagination_termination_reason = "source_total_mismatch"
            self._update_metrics()
            return []

        self.page_sizes = [len(payload)]
        self.pagination_complete = True
        self.pagination_termination_reason = "embedded_single_page"
        jobs = self._build_jobs(payload)
        if self.unique_listed_count != self.EXPECTED_SOURCE_TOTAL:
            self.pagination_complete = False
            self.pagination_termination_reason = "unique_source_total_mismatch"
        self._update_metrics()
        return jobs if self.pagination_complete and self.detail_complete else []


__all__ = ["TaurenCrawler"]
