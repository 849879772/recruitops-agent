"""Dedicated crawler for Qiyunfang's official 2027 campus job wall."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_MODULE_ID_RE = re.compile(r"^module\d+$")
_TOP_RE = re.compile(r"\btop\s*:\s*([\d.]+)px", re.I)
_ZONE_RE = re.compile(r"openZone\(\s*(\d+)\s*\)")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
_SECTION_LABELS = {"岗位职责", "岗位要求", "任职要求", "加分项", "备注"}
_NON_FORMAL_RE = re.compile(
    r"实习生|实习招聘|日常实习|应届实习|实习专项|提前批|提前招聘|提前选拔|"
    r"社会招聘|社招|往届|202[0-6]届",
    re.I,
)
_CURRENT_INTERNSHIP_RE = re.compile(
    r"连续.{0,8}实习|实习.{0,8}(?:个月|每周|到岗)|"
    r"(?:实习生|实习招聘|日常实习|应届实习|实习专项)",
    re.I,
)


def _clean_text(value: object, *, preserve_lines: bool = False) -> str:
    text = str(value or "").replace("\xa0", " ").replace("\r", "")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines) if preserve_lines else " ".join(lines)


class QiyunfangCrawler(BaseCrawler):
    """Parse the official Qiyunfang campus wall and its popup-zone JDs.

    The page is a Huawei CloudSite static layout.  Job cards live only inside
    ``#fk-packContent1468``; each card's ``查看更多`` link identifies a popup
    zone whose HTML is returned by the official ``module_h.jsp`` endpoint.
    """

    OFFICIAL_HOSTS = {"qiyunfang.com", "www.qiyunfang.com"}
    CAMPUS_PATH = "/h-col-124.html"
    WALL_ID = "fk-packContent1468"
    POPUP_API = "https://www.qiyunfang.com/ajax/module_h.jsp"
    CAMPAIGN_TEXT = "启云方2027届全球校园招聘"
    JD_RAW_LIMIT = 12000
    REQUEST_TIMEOUT = 30
    REQUEST_ATTEMPTS = 3
    RETRY_DELAY = 0.25

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self.cohort = 0
        self.cohort_status = "unknown"
        self.cohort_source = ""
        self.cohort_evidence = ""
        self.campaign_text = ""
        self.expected_total: int | None = None
        self.total_num: int | None = None
        self.pages_fetched = 0
        self.page_count = 0
        self.listed_count = 0
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.detail_expected_total = 0
        self.detail_count = 0
        self.detail_complete = False
        self.detail_api_calls = 0
        self.detail_failures: list[dict[str, str]] = []

    def pagination_metrics(self) -> dict[str, object]:
        return {
            "pages_fetched": self.pages_fetched,
            "page_count": self.page_count,
            "listed_count": self.listed_count,
            "expected_total": self.expected_total,
            "total_num": self.total_num,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
        }

    def detail_metrics(self) -> dict[str, object]:
        return {
            "detail_api_calls": self.detail_api_calls,
            "detail_count": self.detail_count,
            "detail_expected_total": self.detail_expected_total,
            "detail_complete": self.detail_complete,
            "detail_failures": list(self.detail_failures),
        }

    @classmethod
    def _is_official_campus_url(cls, url: str) -> bool:
        parsed = urlsplit(url or "")
        return (
            parsed.netloc.casefold() in cls.OFFICIAL_HOSTS
            and parsed.path.rstrip("/").casefold() == cls.CAMPUS_PATH
        )

    @staticmethod
    def _module_top(module) -> float | None:
        match = _TOP_RE.search(module.get("style", ""))
        return float(match.group(1)) if match else None

    @staticmethod
    def _job_ref_url(url: str, zone_id: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, f"job-ref={zone_id}"))

    @classmethod
    def _project_evidence(cls, page_html: str) -> bool:
        soup = BeautifulSoup(page_html or "", "html.parser")
        page_text = _clean_text(soup.get_text(" ", strip=True))
        compact_page_text = re.sub(r"\s+", "", page_text)
        compact_campaign = re.sub(r"\s+", "", cls.CAMPAIGN_TEXT)
        return compact_campaign in compact_page_text

    @classmethod
    def _is_formal_card(cls, title: str, metadata: str) -> bool:
        context = f"{title} {metadata}"
        return (
            "校招" in metadata
            and "应届生" in metadata
            and not _NON_FORMAL_RE.search(context)
        )

    @classmethod
    def _parse_wall(cls, page_html: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(page_html or "", "html.parser")
        wall = soup.find(id=cls.WALL_ID)
        if wall is None:
            return []

        modules = []
        for module in wall.find_all("div", id=_MODULE_ID_RE):
            modules.append({
                "node": module,
                "top": cls._module_top(module),
                "text": _clean_text(module.get_text(" ", strip=True)),
            })

        title_modules = []
        for item in modules:
            title_node = item["node"].find("b")
            text = item["text"]
            title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
            if (
                title_node is not None
                and item["top"] is not None
                and title
                and title == text
                and title not in _SECTION_LABELS
                and not item["node"].find("a", href=True)
            ):
                title_modules.append({"top": item["top"], "title": title})
        title_modules.sort(key=lambda item: item["top"])

        more_modules = []
        for item in modules:
            anchor = item["node"].find("a", href=True)
            if anchor is None or item["top"] is None:
                continue
            match = _ZONE_RE.search(anchor.get("href", ""))
            if match:
                more_modules.append({"top": item["top"], "zone_id": match.group(1)})
        more_modules.sort(key=lambda item: item["top"])

        rows = []
        seen_zones = set()
        for index, more in enumerate(more_modules):
            prior_titles = [item for item in title_modules if item["top"] <= more["top"]]
            if not prior_titles:
                continue
            title_item = max(prior_titles, key=lambda item: item["top"])
            zone_id = more["zone_id"]
            if zone_id in seen_zones:
                continue
            seen_zones.add(zone_id)

            next_title_top = next(
                (
                    item["top"]
                    for item in title_modules
                    if item["top"] > title_item["top"]
                ),
                float("inf"),
            )
            metadata_candidates = [
                item["text"]
                for item in modules
                if (
                    item["top"] is not None
                    and title_item["top"] <= item["top"] < next_title_top
                    and "校招" in item["text"]
                    and "应届生" in item["text"]
                )
            ]
            metadata = metadata_candidates[0] if metadata_candidates else ""
            if not cls._is_formal_card(title_item["title"], metadata):
                continue

            city = metadata.split("▏", 1)[0].strip() if metadata else ""
            date_match = _DATE_RE.search(metadata)
            rows.append({
                "title": title_item["title"],
                "city": city,
                "published_at": date_match.group(1) if date_match else "",
                "zone_id": zone_id,
            })
        return rows

    @classmethod
    def _parse_popup(cls, payload: dict, zone_id: str) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "", ""
        if not payload.get("success"):
            return "", ""
        rt_info = payload.get("rtInfo")
        if isinstance(rt_info, str):
            try:
                rt_info = json.loads(rt_info)
            except json.JSONDecodeError:
                return "", ""
        if not isinstance(rt_info, dict):
            return "", ""

        dom = ""
        for item in rt_info.get("moduleDomList") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("moduleId")) == str(zone_id):
                dom = str(item.get("dom") or "")
                break
        if not dom:
            return "", ""

        soup = BeautifulSoup(dom, "html.parser")
        title = ""
        for node in soup.find_all("b"):
            candidate = _clean_text(node.get_text(" ", strip=True))
            if candidate and candidate not in _SECTION_LABELS:
                title = candidate
                break
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        jd_raw = _clean_text(soup.get_text("\n", strip=True), preserve_lines=True)
        return title, jd_raw[: cls.JD_RAW_LIMIT]

    def _new_session(self):
        return requests.Session()

    def _request(self, session, method: str, url: str, *, data: dict | None = None):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.careers_url,
        }
        kwargs = {
            "headers": headers,
            "timeout": self.REQUEST_TIMEOUT,
            "verify": False,
        }
        if data is not None:
            kwargs["data"] = data
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                response = getattr(session, method)(url, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.warning(
                        "[%s] Qiyunfang %s request failed after %d attempts: %s",
                        self.company_name,
                        method,
                        self.REQUEST_ATTEMPTS,
                        exc,
                    )
                    return None
                time.sleep(self.RETRY_DELAY * attempt)
        return None

    def _job(self, row: dict[str, str], jd_raw: str) -> dict:
        job = self._make_job(
            title=row["title"],
            city=row["city"],
            job_type="校招",
            jd_url=self._job_ref_url(self.careers_url, row["zone_id"]),
            jd_raw=jd_raw,
            published_at=row["published_at"],
            link_kind="list",
            campaign_text=self.CAMPAIGN_TEXT,
        )
        job.update({
            "cohort": 2027,
            "cohort_status": "confirmed",
            "cohort_source": "官网校招活动页",
            "cohort_evidence": self.CAMPAIGN_TEXT,
            "recruitment_track": "formal",
        })
        return job

    def fetch(self) -> list[dict]:
        self._reset_metrics()
        if not self._is_official_campus_url(self.careers_url):
            self.pagination_termination_reason = "wrong_official_campus_page"
            return []

        session = self._new_session()
        page_response = self._request(session, "get", self.careers_url)
        if page_response is None:
            self.pagination_termination_reason = "list_request_failed"
            return []

        rows = self._parse_wall(page_response.text)
        self.pages_fetched = 1
        self.page_count = 1
        self.listed_count = len(rows)
        self.expected_total = len(rows)
        self.total_num = len(rows)
        self.detail_expected_total = len(rows)
        if not self._project_evidence(page_response.text):
            self.pagination_termination_reason = "cohort_evidence_missing"
            return []
        if not rows:
            self.pagination_termination_reason = "empty_official_job_wall"
            return []
        self.cohort = 2027
        self.cohort_status = "confirmed"
        self.cohort_source = "官网校招活动页"
        self.cohort_evidence = self.CAMPAIGN_TEXT
        self.campaign_text = self.CAMPAIGN_TEXT
        self.pagination_complete = True
        self.pagination_termination_reason = "single_static_page"

        jobs = []
        for row in rows:
            payload = {
                "cmd": "getWafNotCk_getPopupZoneModule",
                "_fresh": "false",
                "_colId": "124",
                "_extId": "undefined",
                "popupZoneId": row["zone_id"],
                "manageMode": "false",
                "_majorColor": "#2b2b2b",
                "_vueStyleGrayTest": "false",
            }
            self.detail_api_calls += 1
            detail_response = self._request(session, "post", self.POPUP_API, data=payload)
            if detail_response is None:
                self.detail_failures.append({
                    "zone_id": row["zone_id"],
                    "title": row["title"],
                    "reason": "popup_request_failed",
                })
                continue
            try:
                detail_title, jd_raw = self._parse_popup(detail_response.json(), row["zone_id"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.detail_failures.append({
                    "zone_id": row["zone_id"],
                    "title": row["title"],
                    "reason": f"popup_parse_failed:{type(exc).__name__}",
                })
                continue
            if (
                not detail_title
                or detail_title.replace(" ", "") != row["title"].replace(" ", "")
                or "岗位职责" not in jd_raw
                or _CURRENT_INTERNSHIP_RE.search(jd_raw)
                or "提前批" in jd_raw
                or "往届" in jd_raw
            ):
                self.detail_failures.append({
                    "zone_id": row["zone_id"],
                    "title": row["title"],
                    "reason": "popup_content_mismatch_or_non_formal",
                })
                continue
            self.detail_count += 1
            jobs.append(self._job(row, jd_raw))

        self.detail_complete = self.detail_count == self.detail_expected_total
        if not self.detail_complete:
            logger.warning(
                "[%s] Qiyunfang detail hydration incomplete: %d/%d",
                self.company_name,
                self.detail_count,
                self.detail_expected_total,
            )
            return []
        return jobs
