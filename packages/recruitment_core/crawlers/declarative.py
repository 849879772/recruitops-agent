"""Replay audited crawler recipes without invoking an LLM.

The onboarding agent may discover a recipe, but production crawling must be a
deterministic operation.  This module gives custom career sites the same core
contract as platform crawlers: stable job IDs/URLs, complete pagination,
campaign scopes, and an expected-total check.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler
from .render import render_page

_JD_SIGNAL_RE = re.compile(
    r"岗位职责|工作职责|职位描述|任职要求|岗位要求|任职资格|工作内容|"
    r"responsibilit|requirement|qualification",
    re.I,
)


def _inline_detail_panel(page_html: str, title: str) -> str:
    soup = BeautifulSoup(page_html or "", "html.parser")
    candidates: list[str] = []
    for node in soup.find_all(string=lambda value: str(value or "").strip() == title):
        parent = node.parent
        for _ in range(10):
            if parent is None:
                break
            text = "\n".join(parent.get_text("\n", strip=True).splitlines()).strip()
            if 80 <= len(text) <= 12000 and _JD_SIGNAL_RE.search(text):
                candidates.append(text)
            parent = parent.parent
    return min(candidates, key=len) if candidates else ""


def _dom_job_title(node) -> str:
    child_texts = [
        " ".join(child.get_text(" ", strip=True).split())
        for child in node.find_all(recursive=False)
    ]
    child_texts = [text for text in child_texts if text]
    if (
        len(child_texts) >= 3
        and 2 <= len(child_texts[0]) <= 90
        and any(re.search(r"20\d{2}[/.-]\d{1,2}[/.-]\d{1,2}", text) for text in child_texts[1:])
    ):
        return child_texts[0]
    return " ".join(node.get_text(" ", strip=True).split())


def json_path_get(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "$").removeprefix("$.").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
        return None
    return current


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or value.get("value") or "")
    return str(value or "")


class DeclarativeRecruitCrawler(BaseCrawler):
    """Execute an evidence-backed API, DOM, or delegated ATS recipe."""

    MAX_PAGES = 50

    def __init__(
        self,
        company_name: str,
        careers_url: str,
        recipe: dict[str, Any] | None = None,
        *,
        json_requester: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
        page_renderer: Callable[..., str] | None = None,
    ):
        super().__init__(company_name, careers_url)
        if recipe is None:
            bundle_path = Path(__file__).resolve().parent.parent / "data" / "crawler_recipes.json"
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            recipe = bundle[company_name]
        self.recipe = recipe
        self._json_requester = json_requester
        self._page_renderer = page_renderer or render_page
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.expected_total = 0

    def fetch(self) -> list[dict]:
        recipe_type = self.recipe.get("type")
        if recipe_type == "api_campaigns":
            return self._fetch_api_campaigns()
        if recipe_type == "html_list":
            return self._fetch_html_list()
        if recipe_type == "dom":
            return self._fetch_dom()
        if recipe_type == "delegate":
            return self._fetch_delegate()
        raise ValueError(f"Unsupported declarative recipe: {recipe_type}")

    def _request_json(self, request: dict[str, Any], body: dict[str, Any]) -> Any:
        if self._json_requester is not None:
            return self._json_requester(dict(request), dict(body))
        method = str(request.get("method") or "GET").upper()
        kwargs: dict[str, Any] = {
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
                **(request.get("headers") or {}),
            },
            "timeout": 30,
        }
        if method == "GET":
            kwargs["params"] = body
        else:
            kwargs["json"] = body
        response = requests.request(method, request["url"], **kwargs)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _page_keys(body: dict[str, Any], pagination: dict[str, Any]) -> tuple[str, str]:
        return str(pagination.get("page_key") or "page"), str(
            pagination.get("size_key") or "pageSize"
        )

    def _fetch_scope(self, scope: dict[str, Any]) -> tuple[list[dict], int, str]:
        request = self.recipe["request"]
        body = dict(request.get("body") or {})
        body.update(scope.get("body_overrides") or {})
        pagination = self.recipe.get("pagination") or {}
        page_key, size_key = self._page_keys(body, pagination)
        page_size = int(body.get(size_key) or pagination.get("page_size") or 30)
        first_page = int(body.get(page_key) or 1)
        rows: list[dict] = []
        expected: int | None = None

        for page_number in range(first_page, self.MAX_PAGES + 1):
            body[page_key] = page_number
            payload = self._request_json(request, body)
            page_rows = json_path_get(payload, self.recipe["items_path"])
            if not isinstance(page_rows, list):
                return rows, expected or len(rows), f"items_path_failed_page_{page_number}"
            page_rows = [row for row in page_rows if isinstance(row, dict)]
            if expected is None:
                total_value = json_path_get(payload, self.recipe.get("total_path") or "")
                parsed_total = int(total_value) if str(total_value or "").isdigit() else None
                expected = (
                    parsed_total
                    if parsed_total and parsed_total >= len(page_rows)
                    and not self.recipe.get("total_is_advisory")
                    else None
                )
            if not page_rows:
                break
            rows.extend(page_rows)
            if expected is not None and len(rows) >= expected:
                break
            if expected is None and len(page_rows) < page_size:
                break
        else:
            return rows, expected or len(rows), "max_pages"

        expected = expected if expected is not None else len(rows)
        reason = "all_pages" if len(rows) >= expected else "short_of_expected_total"
        return rows, expected, reason

    def _detail_payload(self, record: dict[str, Any]) -> dict[str, Any] | None:
        detail = self.recipe.get("detail_api") or {}
        if not detail:
            return None
        field_map = self.recipe["field_map"]
        identifier = record.get(field_map.get("id") or "")
        if identifier in (None, ""):
            return None
        url = str(detail["url_template"]).replace("{id}", str(identifier))
        try:
            payload = self._request_json({"method": "GET", "url": url}, {})
        except (requests.RequestException, RuntimeError, TypeError, ValueError):
            return None
        value = json_path_get(payload, detail.get("record_path") or "$.data")
        return value if isinstance(value, dict) else None

    def _normalize_api_job(self, record: dict[str, Any], scope: dict[str, Any]) -> dict:
        fields = self.recipe["field_map"]
        detail_record = self._detail_payload(record) or {}
        merged = {**record, **detail_record}
        jd_fields = self.recipe.get("detail_api", {}).get("jd_fields") or fields.get("jd") or []
        if isinstance(jd_fields, str):
            jd_fields = [jd_fields]
        jd_parts = [str(merged.get(name) or "").strip() for name in jd_fields]
        jd_raw = "\n".join(part for part in jd_parts if part)
        identifier = str(merged.get(fields.get("id") or "") or "")
        template = str(self.recipe.get("detail_url_template") or "")
        detail_url = template.replace("{id}", identifier) if template else self.careers_url
        early_field = str(scope.get("early_batch_field") or "")
        is_early = bool(early_field and merged.get(early_field) in scope.get("early_batch_values", [1, True, "1"]))
        track_text = "提前批" if is_early else "正式批"
        campaign_text = " ".join(
            part for part in (scope.get("label"), scope.get("evidence"), track_text) if part
        )
        job = self._make_job(
            title=str(merged.get(fields["title"]) or "").strip(),
            city=_text(merged.get(fields.get("city") or "")),
            job_type=str(merged.get(fields.get("job_type") or "") or scope.get("label") or "校招"),
            jd_url=detail_url,
            jd_raw=jd_raw,
            published_at=str(merged.get(fields.get("published_at") or "") or ""),
            campaign_text=campaign_text,
        )
        job.update(
            {
                "source_job_id": identifier,
                "cohort": int(scope["cohort"]),
                "cohort_status": "confirmed",
                "cohort_source": "官方招聘项目",
                "cohort_evidence": str(scope.get("evidence") or scope.get("label") or ""),
                "recruitment_track": "early_batch" if is_early else "formal",
            }
        )
        return job

    def _fetch_api_campaigns(self) -> list[dict]:
        jobs: list[dict] = []
        expected_total = 0
        reasons = []
        for scope in self.recipe.get("scopes") or []:
            if not scope.get("include"):
                continue
            records, expected, reason = self._fetch_scope(scope)
            expected_total += expected
            reasons.append(f"{scope.get('label')}: {reason}")
            jobs.extend(self._normalize_api_job(record, scope) for record in records)
        unique: dict[tuple[str, str], dict] = {}
        for job in jobs:
            unique[(str(job.get("source_job_id") or ""), job["jd_url"])] = job
        self.expected_total = expected_total
        self.pagination_complete = len(unique) >= expected_total and all(
            reason.endswith("all_pages") for reason in reasons
        )
        self.pagination_termination_reason = "; ".join(reasons)
        return list(unique.values())

    def _fetch_dom(self) -> list[dict]:
        interactions = self.recipe.get("interactions") or []
        interaction_texts = [
            str(item.get("text") or "") for item in interactions if item.get("text")
        ]
        listing_url = str(self.recipe.get("listing_url") or self.careers_url)
        html = self._page_renderer(
            listing_url,
            timeout_ms=45000,
            extra_wait_ms=3500,
            scroll_times=8,
            click_texts=interaction_texts,
        )
        soup = BeautifulSoup(html or "", "html.parser")
        jobs = []
        seen = set()
        for node in soup.select(str(self.recipe["title_selector"])):
            title = _dom_job_title(node)
            anchor = node if node.name == "a" else node.find_parent("a", href=True) or node.find("a", href=True)
            url = urljoin(listing_url, str(anchor.get("href") or "")) if anchor else listing_url
            key = (title, url)
            if not title or key in seen:
                continue
            seen.add(key)
            if url != listing_url:
                detail_html = self._page_renderer(
                    url, timeout_ms=35000, extra_wait_ms=1200, scroll_times=0
                )
                detail_text = BeautifulSoup(detail_html or "", "html.parser").get_text("\n", strip=True)
            else:
                detail_html = self._page_renderer(
                    listing_url,
                    timeout_ms=45000,
                    extra_wait_ms=1200,
                    scroll_times=0,
                    click_texts=[*interaction_texts, title],
                )
                detail_text = _inline_detail_panel(detail_html or "", title)
            if not detail_text:
                parent = node
                for _ in range(6):
                    parent = parent.parent
                    if parent is None:
                        break
                    candidate = parent.get_text("\n", strip=True)
                    if 100 <= len(candidate) <= 12000 and _JD_SIGNAL_RE.search(candidate):
                        detail_text = candidate
                        break
            source_id = hashlib.sha256(
                f"{title}\n{detail_text}".encode("utf-8")
            ).hexdigest()[:24]
            job = self._make_job(
                title=title,
                jd_url=url,
                jd_raw=detail_text,
                link_kind="detail" if url != self.careers_url else "list",
            )
            job["source_job_id"] = source_id
            jobs.append(job)
        self.expected_total = len(jobs)
        self.pagination_complete = bool(jobs)
        self.pagination_termination_reason = "single_page"
        return jobs

    def _fetch_html_list(self) -> list[dict]:
        listing_url = str(self.recipe.get("listing_url") or self.careers_url)
        list_selector = str(self.recipe["list_selector"])
        title_selector = str(self.recipe["title_selector"])
        detail_link_selector = str(self.recipe.get("detail_link_selector") or "a[href]")
        jd_selector = str(self.recipe.get("jd_selector") or "")
        next_page_text = str(self.recipe.get("next_page_text") or "").strip()
        next_page_selector = str(self.recipe.get("next_page_selector") or "").strip()
        max_pages = min(int(self.recipe.get("max_pages") or 20), self.MAX_PAGES)
        interactions = [
            str(item.get("text") or "").strip()
            for item in self.recipe.get("interactions") or []
            if str(item.get("text") or "").strip()
        ]
        jobs: list[dict] = []
        seen: set[tuple[str, str]] = set()
        page_number = 0
        termination = "single_page"

        for page_number in range(1, max_pages + 1):
            page_clicks = [*interactions, *([next_page_text] * (page_number - 1))]
            selector_clicks = [next_page_selector] * (page_number - 1) if next_page_selector else []
            html = self._page_renderer(
                listing_url,
                timeout_ms=45000,
                extra_wait_ms=2500,
                scroll_times=4,
                click_texts=page_clicks,
                click_selectors=selector_clicks,
            )
            soup = BeautifulSoup(html or "", "html.parser")
            new_count = 0
            for card in soup.select(list_selector):
                title_node = card.select_one(title_selector)
                title = _dom_job_title(title_node) if title_node is not None else ""
                link_node = card.select_one(detail_link_selector)
                if link_node is None and card.name == "a" and card.get("href"):
                    link_node = card
                detail_url = urljoin(listing_url, str(link_node.get("href") or "")) if link_node else listing_url
                key = (title, detail_url)
                if not title or key in seen:
                    continue
                seen.add(key)
                detail_html = (
                    self._page_renderer(detail_url, timeout_ms=35000, extra_wait_ms=1200, scroll_times=0)
                    if detail_url != listing_url
                    else str(card)
                )
                detail_soup = BeautifulSoup(detail_html or "", "html.parser")
                if jd_selector:
                    jd_nodes = detail_soup.select(jd_selector)
                    jd_raw = "\n".join(node.get_text("\n", strip=True) for node in jd_nodes)
                else:
                    jd_raw = detail_soup.get_text("\n", strip=True)
                source_id = hashlib.sha256(f"{title}\n{detail_url}".encode("utf-8")).hexdigest()[:24]
                job = self._make_job(
                    title=title,
                    jd_url=detail_url,
                    jd_raw=jd_raw[:12000],
                    link_kind="detail" if detail_url != listing_url else "list",
                )
                job["source_job_id"] = source_id
                jobs.append(job)
                new_count += 1
            if not next_page_text and not next_page_selector:
                termination = "single_page"
                break
            if page_number > 1 and new_count == 0:
                termination = "next_page_exhausted"
                break
        else:
            termination = "max_pages"

        self.expected_total = len(jobs)
        self.pagination_complete = bool(jobs) and termination != "max_pages"
        self.pagination_termination_reason = termination
        self.pages_seen = page_number
        return jobs

    def _fetch_delegate(self) -> list[dict]:
        crawler_name = str(self.recipe.get("crawler") or "")
        if crawler_name == "moka":
            from .moka import MokaRecruitCrawler

            crawler = MokaRecruitCrawler(self.company_name, self.recipe["url"])
        elif crawler_name == "beisen":
            from .beisen import BeisenRecruitCrawler

            crawler = BeisenRecruitCrawler(self.company_name, self.recipe["url"])
        elif crawler_name == "feishu":
            from .feishu import GenericFeishuCrawler

            crawler = GenericFeishuCrawler(self.company_name, self.recipe["url"])
        elif crawler_name == "hotjob":
            from .hotjob import HotjobRecruitCrawler

            crawler = HotjobRecruitCrawler(self.company_name, self.recipe["url"])
        else:
            raise ValueError(f"Unsupported delegated crawler: {crawler_name}")
        jobs = crawler.fetch()
        self.expected_total = len(jobs)
        self.pagination_complete = bool(getattr(crawler, "pagination_complete", False))
        self.pagination_termination_reason = str(
            getattr(crawler, "pagination_termination_reason", "unknown")
        )
        return jobs
