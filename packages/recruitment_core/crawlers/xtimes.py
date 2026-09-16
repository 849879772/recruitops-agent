"""芯行纪校园招聘抓取器。"""

import re

from .base import BaseCrawler


class XTimesCrawler(BaseCrawler):
    """Read the official campus page through a text fallback when TLS is unstable."""

    READER_PREFIX = "https://r.jina.ai/"
    _IMAGE_LINE = re.compile(r"^!\[.*?\]\(.*?\)$")
    _CITY_LINE = re.compile(r"^[\u4e00-\u9fff]+(?:[/、][\u4e00-\u9fff]+)*$")

    def fetch(self) -> list[dict]:
        reader_url = f"{self.READER_PREFIX}{self.careers_url}"
        resp = self._get(
            reader_url,
            timeout=60,
            headers={
                "Accept": "text/plain",
                "User-Agent": "Mozilla/5.0",
            },
        )
        if not resp:
            return []
        return self._parse_markdown(resp.text)

    def _parse_markdown(self, markdown: str) -> list[dict]:
        content = (markdown or "").split("Markdown Content:", 1)[-1]
        blocks = re.split(r"(?:^|\n)\s*在线投递\s*(?:\n|$)", content)
        jobs = []
        for block in blocks[1:]:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 3:
                continue
            title = lines[0]
            city = lines[1] if self._CITY_LINE.fullmatch(lines[1]) else ""
            body_start = 2 if city else 1
            body_lines = [
                line for line in lines[body_start:]
                if not self._IMAGE_LINE.fullmatch(line)
            ]
            jd_raw = "\n".join(body_lines).strip()
            if not jd_raw or not re.search(r"工作职责|资格要求|任职要求", jd_raw):
                continue
            jobs.append(
                self._make_job(
                    title=title,
                    city=city.replace("/", "、"),
                    jd_url=self.careers_url,
                    jd_raw=jd_raw,
                    link_kind="list",
                )
            )
        return jobs
