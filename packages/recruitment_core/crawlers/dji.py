import logging

from bs4 import BeautifulSoup

from .base import BaseCrawler, launch_browser

logger = logging.getLogger(__name__)


class DJICrawler(BaseCrawler):
    """DJI formal campus crawler.

    The old we.dji.com campus position page currently points at internship or
    mixed listings. Formal campus jobs are hosted on DJI's Moka site, and the
    listing is paginated, so rendering only the first hash route is incomplete.
    """

    LIST_URL = "https://apply.careers.dji.com/campus-recruitment/dji/143359"
    LIST_ROUTE = LIST_URL + "?locale=zh-CN#/jobs"
    SCROLL_TIMES = 8
    MAX_PAGES = 200

    def fetch(self) -> list[dict]:
        pages = self._render_pages()
        if not pages:
            logger.warning("[%s] render failed", self.company_name)
            return []

        all_jobs: list[dict] = []
        seen: set[str] = set()
        for html in pages:
            self._parse_page(html, all_jobs, seen)

        logger.info("[%s] fetched %d jobs from %d pages", self.company_name, len(all_jobs), len(pages))
        return all_jobs

    def _render_pages(self) -> list[str]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        html_pages: list[str] = []
        with sync_playwright() as p:
            browser = launch_browser(p, headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )

            try:
                page.goto(self.LIST_ROUTE, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector('a[href^="#/job/"]', timeout=30000)

                page_count = min(self._detect_page_count(page), self.MAX_PAGES)
                for page_no in range(1, page_count + 1):
                    if page_no > 1:
                        self._go_to_page(page, page_no)
                    self._scroll_page(page)
                    html_pages.append(page.content())
            except PlaywrightTimeoutError as exc:
                logger.warning("[%s] render timeout: %s", self.company_name, exc)
            finally:
                context.close()
                browser.close()

        return html_pages

    def _detect_page_count(self, page) -> int:
        buttons = page.locator("button.sd-Pagination-item-1cqBB")
        page_numbers = []
        for i in range(buttons.count()):
            text = buttons.nth(i).inner_text(timeout=1000).strip()
            if text.isdigit():
                page_numbers.append(int(text))
        return max(page_numbers, default=1)

    def _go_to_page(self, page, page_no: int) -> None:
        page.locator("button.sd-Pagination-item-1cqBB", has_text=str(page_no)).first.click(timeout=10000)
        page.wait_for_selector('a[href^="#/job/"]', timeout=30000)
        page.wait_for_timeout(500)

    def _scroll_page(self, page) -> None:
        for _ in range(self.SCROLL_TIMES):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

    def _parse_page(self, html: str, all_jobs: list[dict], seen: set[str]) -> None:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.select('a[href^="#/job/"]')

        for a in anchors:
            title_el = a.select_one('[class*="title-"]')
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or len(title) < 2:
                continue

            # Moka needs the campus project and locale before the hash route.
            # Omitting them makes DJI redirect the job route to social hiring.
            href = f"{self.LIST_URL}?locale=zh-CN{a['href']}"
            if href in seen:
                continue
            seen.add(href)

            text = a.get_text(" ", strip=True)
            city = ""
            if "|" in text:
                right = text.split("|", 1)[1].strip().split()
                city = right[0] if right else ""

            all_jobs.append(
                self._make_job(
                    title=title,
                    city=city,
                    jd_url=href,
                    jd_raw=text[:1000],
                )
            )
