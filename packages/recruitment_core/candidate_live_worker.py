"""Disposable live runner for a model-generated declarative crawler candidate."""

from __future__ import annotations

import json
import sys

import requests

from packages.recruitment_core import job_filters
from packages.recruitment_core.crawlers.declarative import DeclarativeRecruitCrawler
from packages.recruitment_core.crawlers.render import render_page


def _render_with_http_fallback(url: str, **kwargs) -> str:
    html = render_page(url, **kwargs)
    if str(html or "").strip():
        return str(html)
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=30,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        crawler = DeclarativeRecruitCrawler(
            str(request["company"]),
            str(request["source_url"]),
            dict(request["recipe"]),
            page_renderer=_render_with_http_fallback,
        )
        jobs = crawler.fetch()
        for job in jobs:
            job["recruitment_track"] = job_filters.recruitment_track(job)
        print(json.dumps({
            "ok": True,
            "jobs": jobs,
            "pagination_complete": bool(crawler.pagination_complete),
            "advertised_total": int(crawler.expected_total or len(jobs)),
            "termination_reason": str(crawler.pagination_termination_reason),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[-1_000:]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
