"""华沿机器人（原大族机器人）校园招聘静态分页。"""

from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler


class HuayanCrawler(BaseCrawler):
    """Parse every page of the official campus recruitment list."""

    MAX_PAGES = 20
    JD_RAW_LIMIT = 1200

    def _page_url(self, page: int) -> str:
        parts = urlsplit(self.careers_url)
        query = parse_qs(parts.query)
        query["type"] = ["60"]
        if page > 1:
            query["pagenum"] = [str(page)]
        else:
            query.pop("pagenum", None)
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query, doseq=True),
            "",
        ))

    def _parse_page(self, html: str, page_url: str) -> tuple[list[dict], int]:
        soup = BeautifulSoup(html, "html.parser")
        jobs = []
        for item in soup.select("li.sec-move"):
            title_node = item.select_one(".item-title")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            if not title:
                continue
            city_node = item.select_one(".item-site")
            detail_node = item.select_one(".item-txt2")
            jobs.append(self._make_job(
                title=title,
                city=city_node.get_text(" ", strip=True) if city_node else "",
                jd_url=page_url,
                jd_raw=(detail_node.get_text("\n", strip=True) if detail_node else "")[: self.JD_RAW_LIMIT],
                link_kind="list",
            ))

        page_numbers = [1]
        for anchor in soup.select("a[href*='pagenum=']"):
            values = parse_qs(urlsplit(anchor.get("href", "")).query).get("pagenum", [])
            if values and values[0].isdigit():
                page_numbers.append(int(values[0]))
        return jobs, min(max(page_numbers), self.MAX_PAGES)

    def fetch(self) -> list[dict]:
        jobs, seen = [], set()
        last_page = 1
        page = 1
        while page <= last_page:
            page_url = self._page_url(page)
            response = self._get(page_url, verify=False)
            if not response:
                break
            response.encoding = response.apparent_encoding or response.encoding
            page_jobs, discovered_last = self._parse_page(response.text, page_url)
            last_page = max(last_page, discovered_last)
            for job in page_jobs:
                key = (job["title"], job["city"])
                if key not in seen:
                    seen.add(key)
                    jobs.append(job)
            page += 1
        return jobs
