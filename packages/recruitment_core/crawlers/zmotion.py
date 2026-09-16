import re

from bs4 import BeautifulSoup

from .base import BaseCrawler
from .render import render_page


class ZmotionCrawler(BaseCrawler):
    """Parse the structured recruitment cards on Zmotion's official site."""

    _SALARY_RE = re.compile(r"\s*年薪\s*\d.*$", re.I)
    _CITY_RE = re.compile(r"工作地点\s*[：:]\s*([^\n]+)")

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.fetch_failed = False

    def fetch(self) -> list[dict]:
        response = self._get(
            self.careers_url,
            verify=False,
            timeout=25,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.zmotion.com.cn/",
            },
        )
        if response:
            response.encoding = response.apparent_encoding or response.encoding
            html = response.text
        else:
            html = render_page(
                self.careers_url,
                wait_for=".zhaopin_1",
                timeout_ms=30000,
                extra_wait_ms=1000,
            ) or ""
        if not html:
            self.fetch_failed = True
            return []
        soup = BeautifulSoup(html, "html.parser")

        jobs: list[dict] = []
        seen: set[str] = set()
        for card in soup.select("div.zhaopin_1"):
            heading = card.find("h5")
            raw_title = heading.get_text(" ", strip=True) if heading else ""
            title = self._SALARY_RE.sub("", raw_title).strip()
            if not title or title in seen:
                continue
            seen.add(title)
            text = card.get_text("\n", strip=True)
            city_match = self._CITY_RE.search(text)
            city = city_match.group(1).strip("。. ") if city_match else ""
            jobs.append(self._make_job(
                title=title,
                city=city[:80],
                jd_url=f"{self.careers_url}#job-{len(jobs) + 1}",
                jd_raw=text[:12000],
                link_kind="list",
            ))
        return jobs
