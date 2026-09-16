import logging
import re

from bs4 import BeautifulSoup

from .base import BaseCrawler
from .render import render_page

logger = logging.getLogger(__name__)


class UnitreeCrawler(BaseCrawler):
    """宇树招聘 (https://www.unitree.com/careers/)，Nuxt 3 SPA。

    渲染后真实结构（验证于 2026-05）：
        <a href="/position/<ID>">
          <p class="title">岗位名 <span class="icon hot">热招</span></p>
          <p class="base-info">城市 | 类型 | 部门</p>
          <div class="duty">...JD...</div>
        </a>

    首跑前可用 `playwright codegen https://www.unitree.com/careers/` 核实。
    """

    POSITION_HREF_RE = re.compile(r"/position/\d+")

    def fetch(self) -> list[dict]:
        html = render_page(
            self.careers_url,
            wait_for='a[href^="/position/"] p.title',
            extra_wait_ms=1500,
            timeout_ms=45000,
        )
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        anchors = [
            a for a in soup.find_all("a", href=True)
            if self.POSITION_HREF_RE.search(a["href"])
        ]
        if not anchors:
            logger.warning("[宇树科技] 渲染成功但未匹配到 /position/ 链接")
            return []

        jobs = []
        for a in anchors:
            title_el = a.find("p", class_="title")
            if not title_el:
                continue
            # 去掉"热招"等 span 标签，只保留主体文本
            for span in title_el.find_all("span"):
                span.extract()
            title = title_el.get_text(strip=True)
            if not title or len(title) < 2:
                continue

            city = ""
            base_info = a.find("p", class_="base-info")
            if base_info:
                city = base_info.get_text(strip=True).split("|")[0].strip()

            href = a["href"]
            if not href.startswith("http"):
                href = "https://www.unitree.com" + href

            jd_raw = a.get_text(separator=" ", strip=True)

            jobs.append(
                self._make_job(
                    title=title,
                    city=city,
                    jd_url=href,
                    jd_raw=jd_raw,
                )
            )

        logger.info("[宇树科技] 共抓到 %d 个岗位", len(jobs))
        return jobs
