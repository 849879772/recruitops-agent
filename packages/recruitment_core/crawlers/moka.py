"""Moka ATS（app.mokahr.com）通用校招爬虫基类。

师兄清单里 ~92 家用 Moka，URL 形如：
    https://app.mokahr.com/campus-recruitment/<slug>/<id>
    https://app.mokahr.com/campus_apply/<slug>/<id>
真正的岗位列表在 hash 路由 `#/jobs` 下（客户端渲染），DOM 结构一致：
    <a href="#/job/<uuid>"> ... <div class="...title...">标题</div> ... </a>
每个岗位有唯一的 #/job/<uuid>，拼成 jd_url（保证 upsert 不塌缩）。

子类无需覆盖任何东西——careers_url 即 Moka 落地页，基类自动跳 #/jobs 抓取。
"""
import html as html_lib
import json
import logging
import math
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler, launch_browser
from .render import render_page

logger = logging.getLogger(__name__)


class MokaRecruitCrawler(BaseCrawler):
    EXTRA_WAIT_MS = 6000
    SCROLL_TIMES = 8
    # Moka list cards frequently embed the complete responsibilities and
    # requirements. Keep that text so a 300-character prefix is not mistaken
    # for an already complete JD and sent to scoring without hydration.
    JD_RAW_LIMIT = 12000
    PAGE_SIZE = 30
    MAX_PAGES = 30
    HARD_TIMEOUT_MS = 120000
    GOTO_TIMEOUT_MS = 45000
    _LIST_SELECTOR = '[class^="jobs-"], [class*=" jobs-"]'
    _JOB_SELECTOR = (
        '[class^="jobs-"] a[href*="#/job/"], '
        '[class*=" jobs-"] a[href*="#/job/"]'
    )
    _JOB_HREF_RE = re.compile(r"#/job/[^/?#&]+", re.I)
    _INIT_JOB_KEYS = (
        "jobs", "jobList", "job_list", "positions", "positionList",
        "position_list", "jobPosts", "job_post_list", "list", "rows",
        "items", "data",
    )
    _COUNT_ATTRIBUTES = frozenset({
        "data-total", "data-total-count", "data-total-number",
        "data-result-count", "data-results", "data-job-count",
        "data-jobs-count", "data-position-count", "data-count",
    })
    _COUNT_NODE_RE = re.compile(
        r"(?:count|total|result|pagination|number|数量|总数|职位数|岗位数|结果)",
        re.I,
    )
    _DELIVERY_COUNT_RE = re.compile(
        r"投递|申请|可投|限额|最多|个月|month|delivery|apply",
        re.I,
    )
    _YEAR_CONTEXT_RE = re.compile(r"(?:19|20)\d{2}\s*(?:届|年|年度)")
    _AD_COUNT_RE = re.compile(
        r"\bad(?:s)?\b|广告|advert|banner|promo|promotion|推荐位|宣传",
        re.I,
    )
    _NON_CITY_INFO = {
        "全职", "兼职", "校招", "校园招聘", "社会招聘", "社招",
        "实习", "实习生", "应届生", "正式岗",
    }
    _CITY_TOKENS = (
        "北京", "上海", "天津", "重庆", "深圳", "广州", "杭州", "南京",
        "苏州", "成都", "武汉", "西安", "长沙", "合肥", "厦门", "宁波",
        "青岛", "济南", "郑州", "无锡", "常州", "东莞", "佛山", "珠海",
        "福州", "南昌", "昆明", "沈阳", "大连", "长春", "哈尔滨",
        "石家庄", "太原", "乌鲁木齐", "呼和浩特", "海口", "兰州",
        "西宁", "银川", "贵阳", "南宁", "全国", "海外", "中国",
    )
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    _STEALTH_SCRIPT = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    """

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self._reset_pagination_state()

    def _reset_pagination_state(self) -> None:
        self.pagination_complete = False
        self.pagination_termination_reason = "not_started"
        self.pages_seen = 0
        self.pages_fetched = 0
        self.advertised_total = None
        self.expected_total = None
        self.total_pages = None
        self.has_more = False
        self.resolved_source_url = ""
        self.fetch_failed = False
        self.pagination_evidence: list[dict] = []

    @staticmethod
    def _clean_url(value: str) -> str:
        return html_lib.unescape(str(value or "")).strip()

    def _base_url(self) -> str:
        # Branded domains can redirect to a tenant/project path before rendering.
        parts = urlsplit(self._clean_url(self.resolved_source_url or self.careers_url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    @classmethod
    def _is_city_info(cls, value: str) -> bool:
        if not value or value in cls._NON_CITY_INFO:
            return False
        return (
            any(token in value for token in cls._CITY_TOKENS)
            or "省" in value
            or "市" in value
        )

    def _jobs_url(self, page: int = 1) -> str:
        """Preserve project filters while changing only the Moka page number."""
        parts = urlsplit(self._clean_url(self.careers_url))
        fragment = parts.fragment
        route, _, query = fragment.partition("?")
        if route.strip("/").lower() != "jobs":
            route = "/jobs"
        else:
            route = "/jobs"
        params = dict(parse_qsl(query, keep_blank_values=True))
        params["page"] = str(page)
        params.setdefault("anchorName", "jobsList")
        fragment = f"{route}?{urlencode(params)}"
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, parts.query, fragment)
        )

    @staticmethod
    def _json_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and value.is_integer() and value >= 0:
            return int(value)
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if value.isdigit():
                return int(value)
        return None

    @classmethod
    def _extract_init_data(cls, html: str) -> dict | None:
        soup = BeautifulSoup(html or "", "html.parser")
        node = soup.select_one("#init-data")
        if node is None:
            return None
        raw = node.get("value") or node.get_text("", strip=True)
        if not raw:
            return None
        try:
            data = json.loads(html_lib.unescape(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _init_total(cls, init_data: dict | None) -> int | None:
        return cls._init_total_for_job_ids(init_data)

    @classmethod
    def _init_job_rows(cls, value: dict) -> list | None:
        if not isinstance(value, dict):
            return None
        for key in cls._INIT_JOB_KEYS:
            rows = value.get(key)
            while isinstance(rows, dict):
                nested = None
                for nested_key in cls._INIT_JOB_KEYS:
                    candidate = rows.get(nested_key)
                    if isinstance(candidate, (dict, list)):
                        nested = candidate
                        break
                if nested is None or nested is rows:
                    break
                rows = nested
            if isinstance(rows, list):
                return rows
        return None

    @classmethod
    def _init_row_ids(cls, rows: list | None) -> set[str]:
        if not isinstance(rows, list):
            return set()
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = (
                row.get("id") or row.get("uuid") or row.get("jobId")
                or row.get("job_id") or row.get("positionId")
                or row.get("position_id")
            )
            value = cls._text_value(value)
            if value:
                ids.add(value)
        return ids

    @classmethod
    def _init_total_candidates(cls, init_data: dict | None) -> list[dict]:
        if not isinstance(init_data, dict):
            return []

        candidates = []

        def visit(value: dict, path: tuple[str, ...] = ()) -> None:
            stats = value.get("jobStats") or value.get("job_stats")
            total = None
            if isinstance(stats, dict):
                for key in ("total", "count", "totalCount", "total_count"):
                    total = cls._json_int(stats.get(key))
                    if total is not None:
                        break
            rows = cls._init_job_rows(value)
            if total is None and rows is not None:
                for key in ("total", "count", "totalCount", "total_count"):
                    total = cls._json_int(value.get(key))
                    if total is not None:
                        break
            if total is not None:
                row_ids = cls._init_row_ids(rows)
                candidates.append({
                    "path": path,
                    "total": total,
                    "row_ids": row_ids,
                })
            for key, nested in value.items():
                if isinstance(nested, dict):
                    visit(nested, path + (str(key),))

        visit(init_data)
        return candidates

    @classmethod
    def _init_total_for_job_ids(
        cls, init_data: dict | None, job_ids: set[str] | None = None,
    ) -> int | None:
        candidates = cls._init_total_candidates(init_data)
        if not candidates:
            return None
        job_ids = set(job_ids or ())
        if job_ids:
            with_rows = [candidate for candidate in candidates if candidate["row_ids"]]
            if with_rows:
                matching = [
                    candidate for candidate in with_rows
                    if candidate["row_ids"] & job_ids
                ]
                if not matching:
                    return None
                best_overlap = max(
                    len(candidate["row_ids"] & job_ids) for candidate in matching
                )
                candidates = [
                    candidate for candidate in matching
                    if len(candidate["row_ids"] & job_ids) == best_overlap
                ]
            else:
                candidates = [candidate for candidate in candidates if not candidate["row_ids"]]
        root_candidates = [candidate for candidate in candidates if not candidate["path"]]
        if len(root_candidates) == 1:
            return root_candidates[0]["total"]
        if len(candidates) == 1:
            return candidates[0]["total"]
        totals = {candidate["total"] for candidate in candidates}
        return totals.pop() if len(totals) == 1 else None

    @classmethod
    def _init_has_job_binding(
        cls, init_data: dict | None, job_ids: set[str],
    ) -> bool:
        return bool(
            job_ids
            and any(
                candidate["row_ids"] & job_ids
                for candidate in cls._init_total_candidates(init_data)
            )
        )

    @staticmethod
    def _text_value(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ("name", "label", "text", "value", "cityName"):
                text = MokaRecruitCrawler._text_value(value.get(key))
                if text:
                    return text
        if isinstance(value, (list, tuple)):
            return ", ".join(
                text for text in (MokaRecruitCrawler._text_value(item) for item in value)
                if text
            )
        return ""

    @classmethod
    def _embedded_jobs(cls, init_data: dict | None, base_url: str,
                       seen: set[str], make_job) -> list[dict]:
        if not init_data:
            return []
        rows = init_data.get("jobs") or init_data.get("jobList") or init_data.get("job_list")
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("list") or rows.get("rows")
        if not isinstance(rows, list):
            return []

        jobs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            job_id = row.get("id") or row.get("uuid") or row.get("jobId") or row.get("job_id")
            job_id = cls._text_value(job_id)
            title = cls._text_value(
                row.get("title") or row.get("jobName") or row.get("job_name")
                or row.get("positionName") or row.get("name")
            )
            if not job_id or not title or len(title) < 2:
                continue
            if job_id in seen:
                continue
            city = cls._text_value(
                row.get("city") or row.get("cityName") or row.get("workCity")
                or row.get("location") or row.get("locations")
            )
            employment_type = cls._text_value(
                row.get("employmentType") or row.get("employment_type")
                or row.get("hireType") or row.get("recruitType")
            )
            raw_parts = [
                cls._text_value(row.get(key))
                for key in (
                    "description", "jobDescription", "job_description", "content",
                    "detail", "requirement", "requirements",
                )
            ]
            jd_raw = "\n".join(dict.fromkeys(part for part in raw_parts if part))[: cls.JD_RAW_LIMIT]
            jd_url = f"{base_url}#/job/{job_id}"
            job = make_job(
                title=title,
                city=city,
                job_type=employment_type or "校招",
                jd_url=jd_url,
                jd_raw=jd_raw,
            )
            job["employment_type"] = employment_type
            job["source_list_url"] = base_url
            job["source_job_id"] = job_id
            seen.add(job_id)
            jobs.append(job)
        return jobs

    def _parse_page(self, html: str, seen: set[str]) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        base = self._base_url().split("?")[0]
        jobs = []
        list_container = self._job_list_scope(soup) or self._original_list_container(soup)
        anchors = (list_container or soup).find_all("a", href=True)
        for a in anchors:
            href = self._clean_url(a["href"])
            m = re.search(r"#/job/([^/?#&]+)", href, re.I)
            if not m:
                continue
            uuid = m.group(1)
            if uuid in seen:
                continue
            # 标题：优先取 class 含 title 的元素；否则取锚点文本去掉"发布于…"
            title_el = a.find(lambda t: t.has_attr("class")
                              and any("title" in c.lower() for c in t["class"]))
            title = (title_el.get_text(" ", strip=True) if title_el
                     else re.split(r"发布于", a.get_text(" ", strip=True))[0]).strip()
            if not title or len(title) < 2:
                continue
            city = ""
            employment_type = ""
            info = a.find(
                class_=lambda classes: classes and any(
                    str(name).startswith("info-")
                    for name in (
                        classes if isinstance(classes, list) else [classes]
                    )
                )
            )
            if info:
                info_fields = [
                    node.get_text(" ", strip=True)
                    for node in info.find_all(
                        class_=lambda classes: classes and any(
                            "hiddenContent" in str(name)
                            for name in (
                                classes
                                if isinstance(classes, list)
                                else [classes]
                            )
                        )
                    )
                ]
                info_fields = [value for value in info_fields if value]
                employment_type = next(
                    (
                        value
                        for value in info_fields
                        if value in self._NON_CITY_INFO
                        or re.search(r"实习|intern|兼职|全职", value, re.I)
                    ),
                    "",
                )
                for value in reversed(info_fields):
                    if self._is_city_info(value):
                        city = value
                        break
            jd_url = f"{base}#/job/{uuid}"
            jd_raw = a.get_text(" ", strip=True)[: self.JD_RAW_LIMIT]
            job = self._make_job(
                title=title,
                city=city,
                job_type=employment_type or "校招",
                jd_url=jd_url,
                jd_raw=jd_raw,
            )
            job["employment_type"] = employment_type
            job["source_list_url"] = base
            job["source_job_id"] = uuid
            seen.add(uuid)
            jobs.append(job)
        return jobs

    @classmethod
    def _result_count(cls, html: str) -> int | None:
        soup = BeautifulSoup(html or "", "html.parser")
        scopes = cls._job_list_scopes(soup)
        scope = cls._job_list_scope(soup)
        if scope is None and len(scopes) > 1:
            return None
        job_ids = cls._scope_job_ids(scope)
        init_data = cls._extract_init_data(html)
        init_total = cls._init_total_for_job_ids(init_data, job_ids)
        dom_total = cls._dom_result_count(soup, scope, job_ids)
        if init_total is not None and (
            dom_total is None or cls._init_has_job_binding(init_data, job_ids)
        ):
            return init_total
        return dom_total

    @staticmethod
    def _dom_node_hidden(node) -> bool:
        current = node
        while getattr(current, "name", None) not in (None, "[document]"):
            if (
                current.has_attr("hidden")
                or current.get("aria-hidden") == "true"
                or re.search(
                    r"(?:display|visibility)\s*:\s*(?:none|hidden)",
                    current.get("style", ""),
                    re.I,
                )
            ):
                return True
            current = current.parent
        return False

    @classmethod
    def _job_list_scopes(cls, soup: BeautifulSoup) -> list:
        containers = []
        for node in soup.select(cls._LIST_SELECTOR):
            if cls._dom_node_hidden(node):
                continue
            if any(existing is node or existing in node.parents for existing in containers):
                continue
            containers.append(node)
        with_jobs = [
            node for node in containers
            if node.find("a", href=cls._JOB_HREF_RE) is not None
        ]
        return with_jobs or containers

    @classmethod
    def _job_list_scope(cls, soup: BeautifulSoup):
        scopes = cls._job_list_scopes(soup)
        if len(scopes) == 1:
            return scopes[0]
        active = [scope for scope in scopes if cls._scope_is_current(scope)]
        return active[0] if len(active) == 1 else None

    @classmethod
    def _scope_is_current(cls, scope) -> bool:
        current = scope
        while getattr(current, "name", None) not in (None, "[document]"):
            for name in ("data-active", "data-current", "aria-current"):
                value = str(current.get(name) or "").casefold()
                if value in {"true", "1", "yes", "page", "location", "current"}:
                    return True
            classes = current.get("class", [])
            if any(
                str(value).casefold() in {"active", "selected", "current", "is-active", "is-current"}
                for value in (classes if isinstance(classes, list) else [classes])
            ):
                return True
            current = current.parent
        return False

    @staticmethod
    def _original_list_container(soup: BeautifulSoup):
        return soup.find(
            class_=lambda classes: classes and any(
                str(name).startswith("jobs-")
                for name in (classes if isinstance(classes, list) else [classes])
            )
        )

    @classmethod
    def _scope_job_ids(cls, scope) -> set[str]:
        if scope is None:
            return set()
        job_ids = set()
        for anchor in scope.find_all("a", href=True):
            match = cls._JOB_HREF_RE.search(cls._clean_url(anchor["href"]))
            if match:
                job_ids.add(match.group(0).rsplit("/", 1)[-1])
        return job_ids

    @classmethod
    def _related_count_nodes(cls, scope) -> list[tuple[object, str]]:
        if scope is None:
            return []
        entries = []
        seen = set()

        def add(node, relation: str, descendants: bool = True) -> None:
            if getattr(node, "name", None) is None:
                return
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
            entries.append((node, relation))
            if descendants:
                for child in node.find_all(True):
                    add(child, relation, descendants=False)

        add(scope, "inside")
        current = scope
        for _ in range(2):
            for sibling in (*current.previous_siblings, *current.next_siblings):
                if getattr(sibling, "name", None) is None:
                    continue
                is_another_list = (
                    sibling.find("a", href=cls._JOB_HREF_RE) is not None
                    or sibling.select_one(cls._LIST_SELECTOR) is not None
                )
                add(sibling, "adjacent", descendants=not is_another_list)
            current = current.parent
            if getattr(current, "name", None) in (None, "[document]"):
                break
        return entries

    @classmethod
    def _count_node_context(cls, node) -> str:
        values = []
        for name in (
            "id", "class", "role", "aria-label", "data-testid",
            "data-test", "data-name",
        ):
            value = node.get(name)
            if value:
                values.append(" ".join(value) if isinstance(value, list) else str(value))
        return " ".join(values)

    @classmethod
    def _count_from_text(cls, value: str, *, semantic: bool) -> tuple[int | None, int]:
        text = " ".join(str(value or "").split())
        if not text or len(text) > 240:
            return None, 0
        if cls._DELIVERY_COUNT_RE.search(text) or cls._YEAR_CONTEXT_RE.search(text):
            return None, 0
        patterns = (
            (
                r"(?:共|合计|总计|找到|筛选(?:到|出)?|职位筛选)\s*"
                r"([\d,]{1,7})\s*(?:个|条)?\s*"
                r"(?:结果|职位|岗位|工作|jobs?|positions?)",
                8,
            ),
            (r"(?<![\d,])([\d,]{1,7})\s*(?:个|条)?\s*结果", 7),
            (r"(?:职位|岗位|工作|jobs?|positions?)\s*[（(]\s*([\d,]{1,7})\s*[)）]", 7),
            (r"开启新的工作\s*[（(]\s*([\d,]{1,7})\s*[)）]", 7),
            (r"(?<![\d,])([\d,]{1,7})\s*(?:个|条)\s*(?:职位|岗位|工作)", 3),
        )
        for pattern, quality in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                total = cls._json_int(match.group(1).replace(",", ""))
                if total is not None and (quality >= 7 or semantic):
                    return total, quality
        if semantic and re.fullmatch(r"[\d,]{1,7}", text):
            return cls._json_int(text.replace(",", "")), 4
        return None, 0

    @classmethod
    def _dom_result_count(cls, soup: BeautifulSoup, scope=None, job_ids=None) -> int | None:
        scope = scope or cls._job_list_scope(soup)
        if scope is None:
            return None
        candidates = []
        for order, (node, relation) in enumerate(cls._related_count_nodes(scope)):
            if cls._dom_node_hidden(node):
                continue
            context = cls._count_node_context(node)
            if (
                node.find_parent("a", href=cls._JOB_HREF_RE)
                or re.search(
                    r"detail|description|requirement|responsib|content|详情|职责|要求",
                    context,
                    re.I,
                )
            ):
                continue
            text = "" if node is scope else node.get_text(" ", strip=True)
            blocked = (
                cls._DELIVERY_COUNT_RE.search(f"{context} {text}")
                or cls._YEAR_CONTEXT_RE.search(text)
                or cls._AD_COUNT_RE.search(f"{context} {text}")
            )
            if blocked:
                continue
            semantic = bool(cls._COUNT_NODE_RE.search(context))
            for attr_name in cls._COUNT_ATTRIBUTES:
                value = cls._json_int(node.get(attr_name))
                if value is None:
                    continue
                if attr_name == "data-count" and not semantic and node is not scope:
                    continue
                quality = 9 if "total" in attr_name or "result" in attr_name else 7
                if relation == "inside":
                    quality += 2
                candidates.append((quality, -order, value))
            value, quality = cls._count_from_text(
                text,
                semantic=semantic and "pagination" not in context.casefold(),
            )
            if value is None:
                continue
            quality += 2 if relation == "inside" else 0
            quality += 1 if semantic else 0
            candidates.append((quality, -order, value))
            for attr_name in ("aria-label", "title", "data-label"):
                attr_text = node.get(attr_name)
                if not isinstance(attr_text, str):
                    continue
                value, attr_quality = cls._count_from_text(
                    attr_text, semantic=semantic,
                )
                if value is not None:
                    candidates.append((attr_quality + 2, -order, value))
        if not candidates:
            return None
        best_quality = max(candidate[0] for candidate in candidates)
        best_values = {
            candidate[2] for candidate in candidates
            if candidate[0] == best_quality
        }
        return best_values.pop() if len(best_values) == 1 else None

    @staticmethod
    def _first_job_href(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        container = (
            MokaRecruitCrawler._job_list_scope(soup)
            or MokaRecruitCrawler._original_list_container(soup)
        )
        anchor = (container or soup).find("a", href=re.compile(r"#/job/", re.I))
        return MokaRecruitCrawler._clean_url(anchor.get("href")) if anchor else ""

    @classmethod
    def _settled_total(cls, html: str) -> int | None:
        total = cls._result_count(html)
        if total:
            return total
        if total != 0:
            return None
        soup = BeautifulSoup(html or "", "html.parser")
        job_ids = cls._scope_job_ids(cls._job_list_scope(soup))
        init_data = cls._extract_init_data(html)
        init_total = cls._init_total_for_job_ids(init_data, job_ids)
        # A scoped DOM zero can be the transient hydrated shell; init-data zero
        # is the explicit empty-list evidence used by Moka's API bootstrap.
        return 0 if init_total == 0 and (
            not job_ids or cls._init_has_job_binding(init_data, job_ids)
        ) else None

    @staticmethod
    def _pagination_control(html: str) -> dict:
        soup = BeautifulSoup(html or "", "html.parser")
        variants = (
            (".theme-pagination", '[class*="next-page-"]', '[class*="active-"]'),
            ('[class*="sd-Pagination-pagination-"]',
             '[class*="sd-Pagination-forward-"]', '[class*="sd-Pagination-is-active-"]'),
        )
        for root_selector, next_selector, active_selector in variants:
            for root in soup.select(root_selector):
                if any(
                    node.has_attr("hidden") or node.get("aria-hidden") == "true"
                    or re.search(r"display\s*:\s*none", node.get("style", ""), re.I)
                    for node in [root, *root.parents] if getattr(node, "attrs", None) is not None
                ):
                    continue
                next_node = root.select_one(next_selector)
                active = root.select_one(active_selector)
                if next_node is None or active is None:
                    continue
                current_page = MokaRecruitCrawler._json_int(
                    active.get("data-page") or active.get_text(strip=True)
                )
                disabled = (
                    next_node.has_attr("disabled")
                    or next_node.get("aria-disabled") == "true"
                    or any(
                        name == "disabled" or name.startswith("disabled-")
                        or "-is-disabled-" in name
                        for name in next_node.get("class", [])
                    )
                )
                return {
                    "current_page": current_page,
                    "next_disabled": disabled,
                    "next_selector": f"{root_selector} {next_selector}",
                    "next_html": str(next_node)[:600],
                }
        return {}

    def pagination_metrics(self) -> dict:
        return {
            "pages_seen": self.pages_seen,
            "advertised_total": self.advertised_total,
            "total_pages": self.total_pages,
            "has_more": self.has_more,
            "pagination_complete": self.pagination_complete,
            "pagination_termination_reason": self.pagination_termination_reason,
            "resolved_source_url": self.resolved_source_url,
            "fetch_failed": self.fetch_failed,
            "evidence": list(self.pagination_evidence),
        }

    @staticmethod
    def _remaining_ms(deadline: float, fallback: int) -> int:
        return max(1, min(fallback, int((deadline - time.monotonic()) * 1000)))

    def _fetch_with_reused_browser(self) -> list[dict]:
        from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

        self._reset_pagination_state()
        jobs: list[dict] = []
        seen: set[str] = set()
        expected_total: int | None = None
        page_size: int | None = None
        deadline = time.monotonic() + self.HARD_TIMEOUT_MS / 1000
        source_url = self._jobs_url(1)
        source_path = urlsplit(source_url).path.lower()
        is_mobile = bool(re.search(r"/(?:m)(?:/|$)", source_path))

        with sync_playwright() as playwright:
            browser = launch_browser(
                playwright,
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context_options = dict(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                    if is_mobile else self._USER_AGENT
                ),
                viewport={"width": 390, "height": 844} if is_mobile else {"width": 1366, "height": 768},
                locale="zh-CN",
                ignore_https_errors=True,
            )
            if is_mobile:
                context_options.update(is_mobile=True, has_touch=True, device_scale_factor=2)
            context = browser.new_context(**context_options)
            context.add_init_script(self._STEALTH_SCRIPT)
            browser_page = context.new_page()
            browser_page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )

            try:
                previous_first_href = ""
                next_selector = ""
                for page_number in range(1, self.MAX_PAGES + 1):
                    if time.monotonic() >= deadline:
                        self.pagination_complete = False
                        self.has_more = expected_total is None or len(jobs) < expected_total
                        self.pagination_termination_reason = "hard_timeout"
                        break
                    url = self._jobs_url(page_number)
                    try:
                        if next_selector:
                            browser_page.locator(next_selector).first.click(
                                timeout=self._remaining_ms(deadline, self.GOTO_TIMEOUT_MS),
                            )
                        else:
                            browser_page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=self._remaining_ms(deadline, self.GOTO_TIMEOUT_MS),
                            )
                    except PWTimeout:
                        logger.warning(
                            "[%s] Moka 第 %d 页导航超时，继续检查已渲染内容",
                            self.company_name,
                            page_number,
                        )

                    wait_timeout = self._remaining_ms(deadline, 30000 if not previous_first_href else 20000)
                    try:
                        if previous_first_href:
                            browser_page.wait_for_function(
                                """({previous, selector}) => {
                                    const current = document.querySelector(selector);
                                    return current && current.getAttribute('href') !== previous;
                                }""",
                                arg={"previous": previous_first_href, "selector": self._JOB_SELECTOR},
                                timeout=wait_timeout,
                            )
                        else:
                            browser_page.wait_for_selector(
                                self._JOB_SELECTOR,
                                timeout=wait_timeout,
                                state="attached",
                            )
                    except PWTimeout:
                        logger.warning(
                            "[%s] Moka 第 %d 页岗位列表未在期限内刷新，继续取证",
                            self.company_name,
                            page_number,
                        )

                    browser_page.wait_for_timeout(self._remaining_ms(deadline, 1200))
                    html = browser_page.content()
                    self.pages_seen = page_number
                    self.pages_fetched = page_number
                    self.resolved_source_url = browser_page.url or url
                    init_data = self._extract_init_data(html)
                    current_total = self._settled_total(html)
                    if current_total is not None:
                        expected_total = current_total
                    self.advertised_total = expected_total
                    embedded_count = 0
                    if page_number == 1:
                        embedded = self._embedded_jobs(
                            init_data, self._base_url().split("?")[0], seen, self._make_job
                        )
                        jobs.extend(embedded)
                        embedded_count = len(embedded)
                    before_count = len(jobs)
                    page_jobs = self._parse_page(html, seen)
                    retries = 0
                    # The Moka app clears the old list before its encrypted
                    # search response is rendered. Re-read a bounded number
                    # of times so a transient empty DOM is not a page end.
                    while (
                        not page_jobs
                        and not embedded_count
                        and expected_total != 0
                        and time.monotonic() < deadline
                    ):
                        retries += 1
                        if retries > 5:
                            break
                        browser_page.wait_for_timeout(self._remaining_ms(deadline, 1500))
                        html = browser_page.content()
                        current_total = self._settled_total(html)
                        if current_total is not None:
                            expected_total = current_total
                        self.advertised_total = expected_total
                        page_jobs = self._parse_page(html, seen)
                    control = self._pagination_control(html)
                    self.expected_total = expected_total
                    if not page_jobs and not embedded_count:
                        current_first_href = self._first_job_href(html)
                        if previous_first_href and current_first_href == previous_first_href:
                            self.pagination_complete = False
                            self.has_more = expected_total is None or len(jobs) < expected_total
                            self.pagination_termination_reason = f"page_stalled_{page_number}"
                        elif expected_total == 0:
                            self.pagination_complete = True
                            self.has_more = False
                            self.pagination_termination_reason = "advertised_zero"
                        elif expected_total is not None and len(jobs) >= expected_total:
                            self.pagination_complete = True
                            self.has_more = False
                            self.pagination_termination_reason = "total_reached"
                        else:
                            self.pagination_complete = False
                            self.has_more = True
                            self.pagination_termination_reason = (
                                "zero_total_unconfirmed" if self._result_count(html) == 0
                                else f"empty_page_{page_number}"
                            )
                        self.pagination_evidence.append({
                            "page": page_number,
                            "rows": 0,
                            "collected": len(jobs),
                            "advertised_total": expected_total,
                            "has_more": self.has_more,
                            "retries": retries,
                            "pagination_control": control,
                            "rendered_total": self._result_count(html),
                        })
                        break
                    jobs.extend(page_jobs)

                    observed_page_size = len(page_jobs) or embedded_count
                    if page_number == 1 and not observed_page_size:
                        observed_page_size = len(jobs) - before_count
                    if page_size is None and observed_page_size:
                        page_size = observed_page_size
                    if expected_total is not None and page_size:
                        self.total_pages = math.ceil(expected_total / page_size)
                    self.has_more = expected_total is None or len(jobs) < expected_total
                    self.pagination_evidence.append({
                        "page": page_number,
                        "rows": len(page_jobs),
                        "collected": len(jobs),
                        "advertised_total": expected_total,
                        "has_more": self.has_more,
                        "retries": retries,
                        "pagination_control": control,
                        "rendered_total": self._result_count(html),
                    })

                    previous_first_href = self._first_job_href(html)
                    next_selector = ""
                    if control:
                        if control["current_page"] != page_number:
                            self.pagination_termination_reason = "pagination_page_mismatch"
                            self.has_more = True
                            break
                        if control["next_disabled"]:
                            self.pagination_complete = (
                                expected_total is None or len(jobs) >= expected_total
                            )
                            self.has_more = not self.pagination_complete
                            self.total_pages = page_number
                            self.pagination_termination_reason = (
                                "next_disabled" if self.pagination_complete else "total_shortfall"
                            )
                            self.pagination_evidence[-1]["has_more"] = self.has_more
                            break
                        next_selector = control["next_selector"]
                        if expected_total is not None and len(jobs) >= expected_total:
                            self.has_more = True
                            self.pagination_termination_reason = "pagination_evidence_conflict"
                            self.pagination_evidence[-1]["has_more"] = True
                            break
                    if expected_total is not None and len(jobs) >= expected_total:
                        self.pagination_complete = True
                        self.has_more = False
                        self.pagination_termination_reason = "total_reached"
                        break
                    if expected_total is not None and self.total_pages and page_number >= self.total_pages:
                        self.pagination_complete = len(jobs) >= expected_total
                        self.has_more = len(jobs) < expected_total
                        self.pagination_termination_reason = (
                            "total_reached" if self.pagination_complete else "total_shortfall"
                        )
                        break
                else:
                    self.pagination_complete = False
                    self.has_more = expected_total is None or len(jobs) < expected_total
                    self.pagination_termination_reason = "max_pages"
            finally:
                context.close()
                browser.close()

        logger.info(
            "[%s] Moka 抓到 %d 个岗位（预期 %s）",
            self.company_name,
            len(jobs),
            expected_total if expected_total is not None else "未知",
        )
        return jobs

    def fetch(self) -> list[dict]:
        try:
            return self._fetch_with_reused_browser()
        except ImportError:
            logger.warning(
                "[%s] Playwright 不可用，退回逐页渲染模式",
                self.company_name,
            )
        except Exception as exc:
            logger.error("[%s] Moka 复用浏览器抓取异常: %s", self.company_name, exc)
            self.pagination_complete = False
            self.pagination_termination_reason = "reused_browser_failed"
            self.fetch_failed = True
            return []

        self._reset_pagination_state()
        jobs: list[dict] = []
        seen: set[str] = set()
        html = render_page(
            self._jobs_url(1),
            timeout_ms=45000,
            extra_wait_ms=self.EXTRA_WAIT_MS,
            scroll_times=self.SCROLL_TIMES,
        )
        if html:
            self.resolved_source_url = self._jobs_url(1)
            init_data = self._extract_init_data(html)
            self.advertised_total = self._settled_total(html)
            self.expected_total = self.advertised_total
            jobs.extend(self._embedded_jobs(
                init_data, self._base_url().split("?")[0], seen, self._make_job
            ))
            jobs.extend(self._parse_page(html, seen))
            self.pages_seen = 1
            self.pages_fetched = 1
            control = self._pagination_control(html)
            self.has_more = self.advertised_total is None or len(jobs) < self.advertised_total
            if control and control["current_page"] != 1:
                self.has_more = True
                self.pagination_termination_reason = "pagination_page_mismatch"
            elif control and not control["next_disabled"]:
                self.has_more = True
                self.pagination_termination_reason = "render_single_page_incomplete"
            elif jobs and control and control["next_disabled"]:
                self.pagination_complete = (
                    self.advertised_total is None or len(jobs) >= self.advertised_total
                )
                self.has_more = not self.pagination_complete
                self.total_pages = 1
                self.pagination_termination_reason = (
                    "next_disabled" if self.pagination_complete else "total_shortfall"
                )
            elif self.advertised_total == 0 and not jobs:
                self.pagination_complete = True
                self.has_more = False
                self.pagination_termination_reason = "advertised_zero"
            elif self.advertised_total is not None and len(jobs) >= self.advertised_total:
                self.pagination_complete = True
                self.has_more = False
                self.pagination_termination_reason = "total_reached"
            else:
                self.pagination_complete = False
                self.pagination_termination_reason = "render_single_page_incomplete"
            self.pagination_evidence.append({
                "page": 1,
                "rows": len(jobs),
                "collected": len(jobs),
                "advertised_total": self.advertised_total,
                "rendered_total": self._result_count(html),
                "has_more": self.has_more,
                "pagination_control": control,
            })
        else:
            self.pagination_complete = False
            self.pagination_termination_reason = "render_failed"
            self.fetch_failed = True
        return jobs
