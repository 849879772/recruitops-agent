"""Campus crawler for I-TEK OptoElectronics' self-hosted recruitment site."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseCrawler

logger = logging.getLogger(__name__)


class ItekCrawler(BaseCrawler):
    LIST_PATH = "/front.home.index/schoolList"
    MAX_PAGES = 20
    DETAIL_MARKER = "/front.recruit.recruit/recruitShow/recruit_id/"

    @staticmethod
    def _lines(html: str) -> list[str]:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return [
            " ".join(line.split())
            for line in soup.get_text("\n").splitlines()
            if " ".join(line.split())
        ]

    @classmethod
    def _parse_detail(cls, html: str) -> tuple[str, str, str]:
        soup = BeautifulSoup(html or "", "html.parser")
        title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
        title = re.split(r"-校园招聘", title_text, maxsplit=1)[0].strip()
        lines = cls._lines(html)
        try:
            duty_index = lines.index("岗位职责")
            requirement_index = lines.index("任职要求", duty_index + 1)
        except ValueError:
            return title, "", ""
        end = next(
            (
                index
                for index in range(requirement_index + 1, len(lines))
                if lines[index] in {"尚无简历", "相关推荐岗位", "更多推荐岗位"}
            ),
            len(lines),
        )
        duties = lines[duty_index + 1 : requirement_index]
        requirements = lines[requirement_index + 1 : end]
        detail = "\n".join(
            ["招聘类型：校园招聘", "岗位职责", *duties, "任职要求", *requirements]
        )[:12000]

        city = ""
        if title in lines:
            title_index = lines.index(title)
            for candidate in lines[title_index + 1 : duty_index]:
                if candidate in {"│", "分享", "收藏", "立即申请"}:
                    continue
                if "类" in candidate or "招聘人数" in candidate or "学历" in candidate:
                    continue
                city = candidate[:80]
                break
        return title, city, detail

    def fetch(self) -> list[dict]:
        base = self.careers_url.rstrip("/")
        list_url = urljoin(base + "/", self.LIST_PATH.lstrip("/"))
        detail_urls: list[str] = []
        seen_urls: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            response = self._get(
                list_url,
                params={"page": page},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if not response:
                break
            response.encoding = "utf-8"
            soup = BeautifulSoup(response.text, "html.parser")
            page_urls = []
            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href") or "")
                if self.DETAIL_MARKER not in href:
                    continue
                detail_url = urljoin(list_url, href)
                if detail_url not in seen_urls:
                    seen_urls.add(detail_url)
                    page_urls.append(detail_url)
            if not page_urls:
                break
            detail_urls.extend(page_urls)

        jobs = []
        for detail_url in detail_urls:
            response = self._get(
                detail_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if not response:
                continue
            response.encoding = "utf-8"
            title, city, detail = self._parse_detail(response.text)
            if not title or not detail:
                continue
            jobs.append(
                self._make_job(
                    title=title,
                    city=city,
                    jd_url=detail_url,
                    jd_raw=detail,
                    link_kind="detail",
                )
            )

        logger.info("[%s] 埃科光电校招抓到 %d 个岗位", self.company_name, len(jobs))
        return jobs
