import logging
import os
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

CRAWL_TIMEOUT_ENV = "RECRUITOPS_CRAWL_TIMEOUT_SECONDS"
_crawl_budget: ContextVar[float | None] = ContextVar("crawl_budget", default=None)


def configured_crawl_timeout_seconds() -> float | None:
    """Return the smallest active crawl budget from context or child env."""

    values: list[float] = []
    scoped = _crawl_budget.get()
    if scoped is not None:
        values.append(scoped)
    raw = os.getenv(CRAWL_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0.0
        if parsed > 0:
            values.append(parsed)
    return min(values) if values else None


def effective_crawl_timeout_seconds(default: float) -> float:
    """Clamp an adapter's local budget to the caller-owned crawl deadline."""

    configured = configured_crawl_timeout_seconds()
    if configured is None:
        return max(0.001, float(default))
    return max(0.001, min(float(default), configured))


@contextmanager
def crawl_budget(seconds: float) -> Iterator[None]:
    """Apply a per-attempt budget without leaking it across direct callers."""

    token = _crawl_budget.set(max(0.001, float(seconds)))
    try:
        yield
    finally:
        _crawl_budget.reset(token)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def launch_browser(playwright, **kwargs):
    """Launch the configured local browser; RecruitOps defaults to Microsoft Edge."""

    executable_path = os.getenv("RECRUITOPS_BROWSER_EXECUTABLE_PATH", "").strip()
    channel = os.getenv("RECRUITOPS_BROWSER_CHANNEL", "msedge").strip()
    if os.getenv("RECRUITOPS_ENV") == "desktop-isolated":
        # The isolated runtime owns the executable; never resolve installed Edge.
        if not executable_path:
            raise RuntimeError("desktop_browser_executable_missing")
        kwargs.pop("channel", None)
        kwargs["executable_path"] = executable_path
        return playwright.chromium.launch(**kwargs)
    if executable_path:
        kwargs["executable_path"] = executable_path
    elif channel and channel.casefold() not in {"chromium", "bundled"}:
        kwargs["channel"] = channel
    return playwright.chromium.launch(**kwargs)


def normalize_job_detail_url(url: str, link_kind: str) -> tuple[str, str]:
    """Convert known application intermediaries into browser job-detail URLs."""
    parsed = urlparse(url or "")
    if (
        parsed.netloc.casefold() == "xyz.51job.com"
        and parsed.path.casefold() == "/external/apply.aspx"
    ):
        job_ids = parse_qs(parsed.query).get("jobid") or []
        if job_ids and str(job_ids[0]).isdigit():
            return f"https://jobs.51job.com/all/{job_ids[0]}.html", "detail"
    return url, link_kind


class BaseCrawler:
    REQUEST_ATTEMPTS = 3

    def __init__(self, company_name: str, careers_url: str):
        self.company_name = company_name
        self.careers_url = careers_url

    def fetch(self) -> list[dict]:
        raise NotImplementedError

    def _get(self, url: str, **kwargs) -> Optional[requests.Response]:
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("User-Agent", random.choice(_USER_AGENTS))
        headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        timeout = kwargs.pop("timeout", 15)
        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                resp = requests.get(url, headers=headers, timeout=timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                if attempt == self.REQUEST_ATTEMPTS:
                    logger.error(
                        "[%s] 请求失败 %s（已重试 %d 次）: %s",
                        self.company_name, url, self.REQUEST_ATTEMPTS - 1, e,
                    )
                    return None
                logger.warning(
                    "[%s] 请求失败，准备重试 %d/%d: %s",
                    self.company_name, attempt, self.REQUEST_ATTEMPTS - 1, e,
                )
                time.sleep(0.5 * attempt)

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
        jd_url, link_kind = normalize_job_detail_url(
            jd_url or self.careers_url,
            link_kind,
        )
        return {
            "company": self.company_name,
            "title": title,
            "city": city,
            "job_type": job_type,
            "jd_url": jd_url,
            "jd_raw": jd_raw,
            "published_at": published_at,
            "link_kind": link_kind,
            "source": self.company_name,
            "campaign_text": campaign_text,
        }
