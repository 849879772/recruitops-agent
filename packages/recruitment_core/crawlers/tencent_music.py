"""Tencent Music campus crawler for the current type=10 graduate campaign."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class TencentMusicCampusCrawler(BaseCrawler):
    """Fetch Tencent Music's official type=10 campus positions.

    The list API is scoped by ``type`` but the crawler validates both the URL
    scope and every returned row.  The old campus banner configuration is
    intentionally ignored because it can contain stale cohort text.
    """

    LIST_API = "https://join.tencentmusic.com/api/uc-job/list"
    DETAIL_API = "https://join.tencentmusic.com/api/uc-job/info"
    WEB_CONF_API = "https://join.tencentmusic.com/api/job/list-web-conf"
    ELEMENT_API = "https://join.tencentmusic.com/api/job/element"
    DETAIL_URL_TEMPLATE = "https://join.tencentmusic.com/campus/post-details/?id={id}"

    TARGET_TYPE = 10
    TARGET_LABEL = "应届生"
    PAGE_SIZE = 10
    MAX_PAGES = 100
    JD_RAW_LIMIT = 12000
    REQUEST_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 0.4

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.session = requests.Session()
        self._reset_state()

    def _reset_state(self) -> None:
        self.expected_total: int | None = None
        self.pages_fetched = 0
        self.raw_listed_count = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.termination = "not_started"
        self.pagination_termination_reason = self.termination
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_failures: list[dict[str, str]] = []
        self.cohort = 0
        self.cohort_status = "unknown"
        self.cohort_source = ""
        self.cohort_evidence = ""
        self.campaign_text = ""

    def _set_termination(self, value: str) -> None:
        self.termination = value
        self.pagination_termination_reason = value

    @staticmethod
    def _success(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        code = payload.get("code")
        return code in (None, 200, "200")

    @staticmethod
    def _plain_text(value: object) -> str:
        if value is None:
            return ""
        soup = BeautifulSoup(str(value), "html.parser")
        return "\n".join(
            line.strip()
            for line in soup.get_text("\n").replace("\r", "").splitlines()
            if line.strip()
        )

    @classmethod
    def _field_text(cls, value: object) -> str:
        if isinstance(value, dict):
            return " ".join(
                cls._field_text(value.get(key))
                for key in ("label", "info", "desc", "name", "value")
                if value.get(key) not in (None, "")
            ).strip()
        if isinstance(value, list):
            return " ".join(cls._field_text(item) for item in value).strip()
        return " ".join(str(value or "").split())

    @staticmethod
    def _is_target_type(value: object) -> bool:
        if isinstance(value, bool):
            return False
        try:
            return int(value) == TencentMusicCampusCrawler.TARGET_TYPE
        except (TypeError, ValueError):
            return str(value or "").strip() == str(
                TencentMusicCampusCrawler.TARGET_TYPE
            )

    @staticmethod
    def _is_truthy(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value or "").strip().casefold() in {
            "1", "true", "yes", "y", "on"
        }

    @classmethod
    def _city_text(cls, value: object) -> str:
        if isinstance(value, list):
            labels = []
            for item in value:
                if isinstance(item, dict):
                    label = item.get("label") or item.get("name") or item.get("value")
                else:
                    label = item
                label = " ".join(str(label or "").split())
                if label and label not in labels:
                    labels.append(label)
            return "、".join(labels)
        return " ".join(str(value or "").split())

    @staticmethod
    def _requested_type(url: str) -> str:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("type") or []
        return values[0].strip() if len(values) == 1 else ""

    def _headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _post_json(self, url: str, body: dict[str, object]) -> dict[str, object] | None:
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    url,
                    json=body,
                    headers=self._headers(referer=self.careers_url),
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else None
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] 腾讯音乐 POST 失败 %s: %s",
                        self.company_name,
                        url,
                        exc,
                    )
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
        return None

    def _get_json(
        self,
        url: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(referer=self.careers_url),
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else None
            except (requests.RequestException, ValueError) as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] 腾讯音乐 GET 失败 %s: %s",
                        self.company_name,
                        url,
                        exc,
                    )
                    return None
                time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
        return None

    @classmethod
    def _evidence_from_config(
        cls, config: object, source: str
    ) -> dict[str, object] | None:
        text = cls._field_text(config)
        if "2027" not in text or "应届生" not in text or "实习" in text:
            return None
        # Keep only the current project label. Do not carry the legacy banner's
        # 2024/2025 graduation range into downstream cohort parsing.
        return {
            "cohort": 2027,
            "cohort_status": "confirmed",
            "cohort_source": source,
            "cohort_evidence": "2027应届生招聘",
            "campaign_text": "2027应届生招聘",
        }

    @classmethod
    def _web_conf_evidence(cls, payload: object) -> dict[str, object] | None:
        if not cls._success(payload):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        for item in data:
            if not isinstance(item, dict) or item.get("key") != "website_recruitment_type":
                continue
            project_data = item.get("data")
            current = (
                project_data.get("10")
                if isinstance(project_data, dict)
                else None
            )
            return cls._evidence_from_config(
                current,
                "官方 website_recruitment_type[10]",
            )
        return None

    @classmethod
    def _element_evidence(cls, payload: object) -> dict[str, object] | None:
        if not cls._success(payload):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        current = (data.get("uc_type") or {}).get("10")
        return cls._evidence_from_config(current, "官方 uc_type[10]")

    def _load_project_evidence(self) -> dict[str, object] | None:
        decision = self._web_conf_evidence(self._get_json(self.WEB_CONF_API))
        if decision:
            return decision
        return self._element_evidence(self._get_json(self.ELEMENT_API))

    def _list_page(self, page: int) -> tuple[list[dict[str, object]], dict[str, object]]:
        body: dict[str, object] = {
            "page": page,
            "ss": self.PAGE_SIZE,
            "type": str(self.TARGET_TYPE),
            "job_class": [],
            "work_city": "",
            "setid": "",
            "keyword": "",
        }
        payload = self._post_json(self.LIST_API, body)
        if not self._success(payload):
            raise RuntimeError("list_api_error")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("list_payload_missing_items")
        meta = data.get("_meta")
        return data["items"], meta if isinstance(meta, dict) else {}

    def _eligible_listing_row(self, row: dict[str, object]) -> bool:
        if not self._is_target_type(row.get("job_type")):
            return False
        if self._is_truthy(row.get("is_advance_approval")):
            return False
        return "实习" not in str(row.get("job_type_descr") or "")

    def _fetch_rows(self) -> list[dict[str, object]]:
        raw_rows: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        page_count: int | None = None

        for page in range(1, self.MAX_PAGES + 1):
            try:
                page_items, meta = self._list_page(page)
            except Exception as exc:  # noqa: BLE001
                self._set_termination(f"list_request_failed_page_{page}")
                logger.warning("[%s] 腾讯音乐第 %d 页失败: %s", self.company_name, page, exc)
                break

            self.pages_fetched = page
            if self.expected_total is None:
                raw_total = meta.get("total_count")
                try:
                    self.expected_total = int(raw_total) if raw_total is not None else None
                except (TypeError, ValueError):
                    self.expected_total = None
            try:
                page_count = int(meta.get("page_count")) if meta.get("page_count") else None
            except (TypeError, ValueError):
                page_count = None

            if not page_items:
                if self.expected_total in (None, len(raw_rows)):
                    self.pagination_complete = True
                    self._set_termination("empty_page_after_total")
                else:
                    self._set_termination(f"empty_page_before_total_{page}")
                break

            for row in page_items:
                if not isinstance(row, dict):
                    continue
                source_id = str(row.get("id") or "").strip()
                if not source_id or source_id in seen_ids:
                    continue
                seen_ids.add(source_id)
                raw_rows.append(row)

            self.raw_listed_count = len(raw_rows)
            if self.expected_total is not None:
                if len(raw_rows) == self.expected_total:
                    self.pagination_complete = True
                    self._set_termination("total_reached")
                    break
                if len(raw_rows) > self.expected_total:
                    self._set_termination("total_mismatch")
                    break
            elif len(page_items) < self.PAGE_SIZE:
                self.pagination_complete = True
                self._set_termination("short_page")
                break

            if page_count is not None and page >= page_count:
                if self.expected_total is None or len(raw_rows) == self.expected_total:
                    self.pagination_complete = True
                    self._set_termination("page_count_reached")
                else:
                    self._set_termination("page_count_total_mismatch")
                break
        else:
            self._set_termination("max_pages")

        self.raw_listed_count = len(raw_rows)
        return [row for row in raw_rows if self._eligible_listing_row(row)]

    def _detail_record(self, source_id: str) -> dict[str, object] | None:
        payload = self._get_json(self.DETAIL_API, params={"id": source_id})
        if not self._success(payload):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or str(data.get("id") or "") != source_id:
            return None
        if not self._is_target_type(data.get("job_type")):
            return None
        if self._is_truthy(data.get("is_advance_approval")):
            return None
        if "实习" in str(data.get("job_type_descr") or ""):
            return None
        return data

    def _make_job_from_detail(
        self,
        row: dict[str, object],
        detail: dict[str, object],
        evidence: dict[str, object],
    ) -> tuple[dict[str, object], bool, str]:
        source_id = str(row.get("id") or "")
        duty = self._plain_text(detail.get("duty"))
        requirement = self._plain_text(detail.get("requirement"))
        parts = []
        if duty:
            parts.extend(["岗位职责", duty])
        if requirement:
            parts.extend(["任职要求", requirement])
        missing = []
        if not duty:
            missing.append("duty")
        if not requirement:
            missing.append("requirement")
        job = self._make_job(
            title=str(detail.get("name") or row.get("name") or "").strip(),
            city=self._city_text(detail.get("work_city") or row.get("work_city")),
            job_type=self.TARGET_LABEL,
            jd_url=self.DETAIL_URL_TEMPLATE.format(id=source_id),
            jd_raw="\n".join(parts)[: self.JD_RAW_LIMIT],
            published_at=str(detail.get("date") or row.get("date") or "").strip(),
            link_kind="detail",
            campaign_text=str(evidence["campaign_text"]),
        )
        job.update(
            {
                "source_job_id": source_id,
                "cohort": evidence["cohort"],
                "cohort_status": evidence["cohort_status"],
                "cohort_source": evidence["cohort_source"],
                "cohort_evidence": evidence["cohort_evidence"],
                "recruitment_track": "formal",
            }
        )
        return job, not missing, ",".join(missing)

    def _hydrate_details(
        self,
        rows: list[dict[str, object]],
        evidence: dict[str, object],
    ) -> list[dict[str, object]]:
        self.detail_expected_total = len(rows)
        jobs: list[dict[str, object]] = []
        for row in rows:
            source_id = str(row.get("id") or "").strip()
            detail = self._detail_record(source_id)
            if detail is None:
                self.detail_failures.append(
                    {
                        "id": source_id,
                        "url": self.DETAIL_URL_TEMPLATE.format(id=source_id),
                        "reason": "missing_or_non_target_detail",
                    }
                )
                continue
            job, complete, missing = self._make_job_from_detail(row, detail, evidence)
            jobs.append(job)
            if complete:
                self.detail_count += 1
            else:
                self.detail_failures.append(
                    {
                        "id": source_id,
                        "url": job["jd_url"],
                        "reason": f"missing_jd:{missing}",
                    }
                )
        self.listed_count = len(jobs)
        self.detail_complete = (
            self.detail_count == self.detail_expected_total
            and not self.detail_failures
        )
        return jobs

    def fetch(self) -> list[dict]:
        self._reset_state()
        if self._requested_type(self.careers_url) != str(self.TARGET_TYPE):
            self._set_termination("invalid_type_scope")
            return []

        evidence = self._load_project_evidence()
        if not evidence:
            self._set_termination("cohort_evidence_missing")
            return []

        self.cohort = int(evidence["cohort"])
        self.cohort_status = str(evidence["cohort_status"])
        self.cohort_source = str(evidence["cohort_source"])
        self.cohort_evidence = str(evidence["cohort_evidence"])
        self.campaign_text = str(evidence["campaign_text"])

        rows = self._fetch_rows()
        return self._hydrate_details(rows, evidence)
