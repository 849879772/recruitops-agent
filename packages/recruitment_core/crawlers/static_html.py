import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler

_JOB_KW = re.compile(
    r"工程师|研发|开发|算法|测试|设计|设计师|管培|培训生|专员|研究员|技术员|"
    r"engineer|developer|trainee|analyst",
    re.I,
)
_NOISE = re.compile(
    r"首页|关于|联系|招聘|社会招聘|校园招聘|加入我们|岗位职责|任职要求|开发者论坛|开发者平台|"
    r"职位描述|工作地点|发布时间|申请职位|投递|邮箱|电话|地址|Copyright|"
    r"有限责任公司|股份有限公司|集团有限公司|招贤纳士|简称|公司简介|"
    r"^负责|熟悉|掌握|经验|使用|配合|了解|支持工作|开发流程|文档|技能|"
    r"岗位等|类岗位|高级架构师|根据|完成|保证|规格|专业：|工具：|辅助|撰写|"
    r"理论基础|依照|提供|需求|客户方案|流程|热爱|原理图|维护|完善|制定|编写|"
    r"RTL|EDA|有音|编解码|H\\.264",
    re.I,
)
_PREFIX = re.compile(r"^[\s　·•●○\-—:：]+")
_NUMBERED_TEXT = re.compile(r"^\d+[、.，,]")
_HISTORY_TEXT = re.compile(r"^(19|20)\d{2}年|研发出|研发成功|成功开发出")
_SENTENCE_TEXT = re.compile(r"[，,；;。]$")
_SECTION_TEXT = re.compile(
    r"^(研发中心|研发实力|研发平台|职能类型|招聘类型|职位类型|所属部门|工程与设计服务|"
    r"开发套件和开发板|参考设计|设计服务|科技研发|开发技术类|"
    r"[\w\u4e00-\u9fff]+&[\w\u4e00-\u9fff]+类|[\w\u4e00-\u9fff]+类|[\w\u4e00-\u9fff]+部)$"
)
_SOCIAL_TITLE_TEXT = re.compile(r"高级工程师|资深|专家|主管|经理|总监")
_NON_JOB_HREF = re.compile(
    r"(?:^|/)(?:about|bbs|developer|forum|news|products?|research|solutions?|technology)(?:/|\.|$)",
    re.I,
)
_HIRING_CARD_SIGNAL = re.compile(
    r"薪资待遇|招聘人数|岗位职责|工作职责|职位描述|任职要求|任职资格|工作地点",
    re.I,
)
_DETAIL_LINK_TEXT = re.compile(r"详情|查看|申请|投递", re.I)
_NON_CAMPUS_CONTEXT = re.compile(r"社会招聘|社招|日常实习|应届实习|实习生")
_NON_JOB_HOSTS = {
    "youtube.com", "www.youtube.com", "linkedin.com", "www.linkedin.com",
    "bilibili.com", "www.bilibili.com", "weibo.com", "www.weibo.com",
}


def _is_non_job_url(url: str) -> bool:
    return urlsplit(url).netloc.casefold() in _NON_JOB_HOSTS


class StaticHtmlCrawler(BaseCrawler):
    """Parse simple static company career pages with visible job-title text."""

    MIN_LEN = 4
    MAX_LEN = 40

    @staticmethod
    def _hiring_card(node):
        """Return the nearest bounded element that looks like one job row/card."""
        current = getattr(node, "parent", None)
        for _ in range(7):
            if current is None:
                break
            text = current.get_text(" ", strip=True)
            if len(text) > 1600:
                break
            if _HIRING_CARD_SIGNAL.search(text):
                return current
            current = getattr(current, "parent", None)
        return None

    def _card_detail_url(self, card) -> str:
        if card is None:
            return ""
        for anchor in card.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not href or href == "#" or href.lower().startswith("javascript:"):
                continue
            text = anchor.get_text(" ", strip=True)
            if not _DETAIL_LINK_TEXT.search(text):
                continue
            resolved = urljoin(self.careers_url, href)
            if (
                resolved == self.careers_url
                or _NON_JOB_HREF.search(urlsplit(resolved).path)
                or _is_non_job_url(resolved)
            ):
                continue
            return resolved
        return ""

    def _clean_title(self, text: str) -> str:
        text = _PREFIX.sub("", re.sub(r"\s+", " ", text or "").strip())
        if not (self.MIN_LEN <= len(text) <= self.MAX_LEN):
            return ""
        if (
            _NUMBERED_TEXT.search(text)
            or _HISTORY_TEXT.search(text)
            or _SENTENCE_TEXT.search(text)
            or _SECTION_TEXT.search(text)
            or _SOCIAL_TITLE_TEXT.search(text)
            or _NOISE.search(text)
            or not _JOB_KW.search(text)
        ):
            return ""
        return text

    def fetch(self) -> list[dict]:
        resp = self._get(
            self.careers_url,
            verify=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "http://www.swid.com.cn/",
            },
        )
        if not resp:
            return []
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        jobs, seen = [], set()
        for anchor in soup.find_all("a", href=True):
            title = self._clean_title(anchor.get_text(" ", strip=True))
            href = (anchor.get("href") or "").strip()
            if not title or title in seen or not href or href == "#" or href.lower().startswith("javascript:"):
                continue
            jd_url = urljoin(self.careers_url, href)
            if (
                jd_url == self.careers_url
                or _NON_JOB_HREF.search(urlsplit(jd_url).path)
                or _is_non_job_url(jd_url)
            ):
                continue
            seen.add(title)
            jobs.append(self._make_job(title=title, jd_url=jd_url, link_kind="detail"))
        for text_node in soup.find_all(string=True):
            parent = text_node.parent
            title = self._clean_title(str(text_node))
            if not title or title in seen:
                continue
            anchor = None
            if parent:
                anchor = parent if parent.name == "a" else parent.find_parent("a")
            if anchor:
                href = (anchor.get("href") or "").strip()
                context = anchor.get_text(" ", strip=True)
                if _NON_CAMPUS_CONTEXT.search(context):
                    continue
                if not href or href == "#" or href.lower().startswith("javascript:"):
                    jd_url = self.careers_url
                    link_kind = "list"
                else:
                    jd_url = urljoin(self.careers_url, href)
                    if (
                        jd_url == self.careers_url
                        or _NON_JOB_HREF.search(urlsplit(jd_url).path)
                        or _is_non_job_url(jd_url)
                    ):
                        continue
                    link_kind = "detail"
            else:
                detail_url = self._card_detail_url(self._hiring_card(text_node))
                jd_url = detail_url or self.careers_url
                link_kind = "detail" if detail_url else "list"
            seen.add(title)
            jobs.append(self._make_job(title=title, jd_url=jd_url, link_kind=link_kind))
        return jobs
