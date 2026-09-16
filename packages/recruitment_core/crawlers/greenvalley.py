"""Digital Green Valley's 51job campaign-site campus crawler."""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)

_JOB_RE = re.compile(
    r"\{\s*lb:\s*'(?P<category>.*?)'\s*,\s*"
    r"gw:\s*'(?P<title>.*?)'\s*,\s*"
    r"ms:\s*'(?P<description>.*?)'\s*,\s*"
    r"yq:\s*'(?P<requirement>.*?)'\s*,\s*"
    r"lj:\s*'(?P<url>.*?)'\s*\}",
    re.S,
)


def _plain_text(value: str) -> str:
    soup = BeautifulSoup(html.unescape(value or ""), "html.parser")
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


class GreenvalleyCrawler(BaseCrawler):
    JD_RAW_LIMIT = 12000

    def fetch(self) -> list[dict]:
        script_url = urljoin(self.careers_url, "js/xz.js")
        response = requests.get(
            script_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": self.careers_url},
            timeout=30,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"

        jobs = []
        seen = set()
        for match in _JOB_RE.finditer(response.text):
            apply_url = html.unescape(match.group("url")).strip()
            if "TYPE=CAMPUSRECRUITMENT" not in apply_url.upper():
                continue
            title = _plain_text(match.group("title"))
            if not title or (title, apply_url) in seen:
                continue
            seen.add((title, apply_url))
            description = _plain_text(match.group("description"))
            requirement = _plain_text(match.group("requirement"))
            parts = []
            if description:
                parts.extend(["职位描述", description])
            if requirement:
                parts.extend(["任职要求", requirement])
            position_id = urlsplit(apply_url).path.rstrip("/").rsplit("/", 1)[-1]
            listing_url = f"{self.careers_url}#job-ref={position_id or len(jobs) + 1}"
            jobs.append(
                self._make_job(
                    title=title,
                    job_type=f"校园招聘 {_plain_text(match.group('category'))}",
                    jd_url=listing_url,
                    jd_raw="\n".join(parts)[: self.JD_RAW_LIMIT],
                    link_kind="list",
                )
            )
        logger.info("[%s] 数字绿土校招页抓到 %d 个正式岗位", self.company_name, len(jobs))
        return jobs
