"""通用渲染爬虫 —— 服务「自建 SPA 校招站」(无公开 API 或 API 带反爬)。

很多公司校招站是自建 SPA，职位列表 API 常带客户端反爬 token(如 bilibili 的
ajSessionId)，裸 requests 抓不到。用 Playwright 渲染真实页面时页面自身 JS 会带上
token、DOM 正常填充，故走「渲染 + 自动选主选择器 + 翻页」绕过。

**自动选主选择器**(避免"抓所有关键词元素"的脏数据)：渲染后统计每个 tag.class
里"像职位名"的直接文本数，职位列表组件会重复出现 N 次(N 远大于筛选/导航噪声)，
取得分最高、且文本足够干净的那个 class 作为唯一选择器，只解析它。

config 用法：crawler: render + careers_url(职位列表页 URL)。无需子类。
若某站结构特殊选不出干净选择器(主选择器命中数 < 阈值)，优雅返空、归人工。
"""
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler, effective_crawl_timeout_seconds, launch_browser
from .tonghuashun import TonghuashunCampusCrawler

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CITY_NAMES = (
    "北京", "上海", "天津", "重庆", "香港", "澳门", "深圳", "广州", "杭州",
    "南京", "苏州", "无锡", "常州", "南通", "徐州", "扬州", "镇江", "昆山",
    "宁波", "温州", "嘉兴", "绍兴", "金华", "台州", "湖州", "武汉", "宜昌",
    "襄阳", "成都", "绵阳", "西安", "合肥", "芜湖", "长沙", "株洲", "郑州",
    "洛阳", "济南", "青岛", "烟台", "潍坊", "威海", "厦门", "福州", "泉州",
    "漳州", "珠海", "东莞", "佛山", "惠州", "中山", "大连", "沈阳", "长春",
    "哈尔滨", "石家庄", "太原", "南昌", "南宁", "海口", "贵阳", "昆明",
    "兰州", "乌鲁木齐", "呼和浩特", "全国", "海外", "国外",
)
_CITY_RE = re.compile(
    rf"({'|'.join(sorted(_CITY_NAMES, key=len, reverse=True))})(?:市)?"
)
_JOB_KW = re.compile(
    r"工程师|工程优化|研发|开发|算法|架构|设计师|实习生|管培|培训生|专员|主管|"
    r"产品经理|运营|测试|分析师|顾问|储备|工艺|科学家|研究员|策划|助理|应届|博士后|"
    r"信息技术|产品设计|岗|trainee|engineer|developer|intern|graduate|analyst",
    re.I,
)
# 明显非职位的噪声(筛选/导航/页脚/说明文案)
_NOISE = re.compile(
    r"筛选|清除|职位类别|工作地点|大职能|仅查看|热门|须知|查看|更多|登录|注册|开发者论坛|开发者平台|开发者区域|"
    r"计划是|面向|推出|全球|高校|在校生|清空|展开|收起|首页|关于|联系|"
    r"招聘流程|投递方式|个人中心|加入我们|了解更多|立即申请|点击|"
    r"校招流程|校招岗位|校招FAQ|FAQ|招聘公告|招聘简章|招聘动态|校招行程|快捷通道|"
    r"应届生招聘|实习生招聘|博士生招聘|进入校招|实习机会|"
    r"产品类|技术类|职能类|运营市场类|研发美术类|运营美术类|岗位类别|"
    r"子类别|业务开发与支持|系统开发管理|系统运维管理|网络规划与投资计划|项目计划管理|网络运营管理|"
    r"开发板|评估板|参考设计|所有开发板|开发工具|测试服务|"
    r"研发中心|开发团队|业务架构|工程中心|工厂中心|需求部门|"
    r"职位描述|岗位职责|任职要求|工作内容|招聘对象|招收对象|毕业时间|"
    r"年及以上|三年及以上|五年及以上|经验者优先|全流程测试能力|大学|"
    r"产品中心|解决方案|了解更多|产品研发|开发成本|协同研发|硬件基础|开箱即用|"
    r"高性能CAE|几何内核|约束求解|数字化仿真|数字化制造|数字化设计|产品开发|"
    r"行业应用|应用验证|信创生态|自主可控|功能成熟|高效替代|合作咨询|人事招聘|"
    r"测试机|通用数字测试机|复杂SoC测试机|DDIC测试机|CIS测试机|半导体测试机|"
    r"摄像头模组测试|测试系统软件开发服务",
    re.I,
)
_NON_JOB_TITLE = re.compile(
    r"成为开发者|开发者(?:社区|论坛|平台|区域|中心)?|开发文档|技术文档|产品文档|"
    r"定制开发服务|开发工具|参考设计|评估板|所有开发板|文档中心|解决方案中心|"
    r"关于我们|联系我们|人才理念|招聘公告|招聘简章|校招FAQ|招聘流程|"
    r"组织架构|组织结构|企业文化|公司简介|公司概况|福利待遇|薪酬福利|"
    r"新闻动态|新闻中心|联系我们|人才发展|人才政策|招聘信息|职位详情|岗位详情|"
    r"^在校/应届$|^本科及以上$|^硕士及以上$|^投资者关系$|^人才培育$",
    re.I,
)
_NON_JOB_ROUTE = re.compile(
    r"(?:^|/)(?:org(?:anization)?|organization|structure|company[-_]?profile|"
    r"about(?:[-_]?us)?|culture|welfare|benefit|contact|news|notice|"
    r"announcement|faq|process|talent[-_]?(?:concept|development|policy))"
    r"(?:[./?#_\-]|$)",
    re.I,
)
_BAD_TITLE_RE = re.compile(r"^\d+[、.，,]|[。；;]$|^20(1\d|2[0-4])-\d{2}-\d{2}|类\s*\|\s*")
_CONTEXT_NOISE = re.compile(
    r"社会招聘|工作经验|三年及以上|五年及以上|博士后工作站|招聘公告|双选会|宣讲会|基层就业|学院就业|"
    r"产品中心|解决方案|了解更多|产品研发|几何内核|约束求解|数字化仿真|数字化制造|信创生态|合作咨询|"
    r"测试机|通用数字测试机|复杂SoC测试机|DDIC测试机|CIS测试机|半导体测试机|摄像头模组测试|"
    r"单位名称|单位所在地|应聘方式|海报|双选会|招聘会",
    re.I,
)
_JD_SIGNAL = re.compile(
    r"岗位职责|职位描述|工作内容|工作职责|任职要求|任职资格|招聘要求|"
    r"responsibilit(?:y|ies)|requirements?|qualifications?",
    re.I,
)
_JOB_CONTEXT_SIGNAL = re.compile(
    r"工作地点|职位类别|岗位类别|招聘人数|发布时间|工作城市|job\s*(?:type|location)|"
    r"position\s*(?:type|location)|应届生|学历|大专|本科|硕士|博士|招聘类型|招聘对象|"
    r"职位性质|工作性质",
    re.I,
)
_CARD_CLASS = re.compile(
    r"(?:^|[-_\s])(?:job|position|post|vacancy|opening|offer|recruit)"
    r"(?:[-_\s]?(?:card|item|items|row|hot|trend|hd))(?=$|[-_\s])|"
    r"(?:^|[-_\s])(?:card|item|items|row)(?:[-_\s](?:job|position|post|vacancy|opening|offer|recruit))(?=$|[-_\s])",
    re.I,
)
_DETAIL_ROUTE = re.compile(
    r"(?:job|position|post|vacan|recruit|career|opening|detail)|"
    r"(?:job|position|post|pid|recruitment)[_-]?(?:id)?=",
    re.I,
)
_APPLY_TEXT = re.compile(r"立即申请|申请职位|投递简历|在线应聘|报名|apply|submit", re.I)
_LOADING_SELECTORS = (
    ".loading", ".is-loading", ".el-loading-mask", ".ant-spin-spinning",
    "[aria-busy='true']", "[data-loading='true']",
)
_PAGING_QUERY_KEYS = frozenset(
    {
        "page", "p", "pageindex", "page_index", "pageno", "page_no", "pagenum",
        "page_num", "currentpage", "current_page", "offset", "start", "limit",
        "pagesize", "page_size", "size",
    }
)
_VOLATILE_QUERY_KEYS = frozenset(
    {"token", "access_token", "authorization", "session", "sessionid", "nonce", "timestamp", "sign"}
)
_POST_DATA_UNSET = object()
_NON_JOB_HOSTS = {
    "youtube.com", "www.youtube.com", "linkedin.com", "www.linkedin.com",
    "bilibili.com", "www.bilibili.com", "weibo.com", "www.weibo.com",
}
_SOURCE_ID_KEYS = frozenset(
    {
        "id", "jobid", "job_id", "jobadid", "job_ad_id", "postid", "post_id",
        "positionid", "position_id", "pid", "recruitmentid", "recruitment_id",
        "advertisementid", "advertisement_id",
    }
)
_PLACEHOLDER_SOURCE_ID_RE = re.compile(
    r"^(?:undefined|null|none|nan|n/?a|unknown|[-_]+)$",
    re.I,
)
_DOM_URGENT_RE = re.compile(r"^(?:急|急招|急聘|hot|urgent)$", re.I)
_DOM_NEGOTIABLE_RE = re.compile(
    r"^(?:面议|薪资面议|salary\s*negotiable|negotiable)$",
    re.I,
)
_DOM_PUBLISH_HINT_RE = re.compile(
    r"发布时间|发布(?:日期|时间)?|更新时间|date\s*posted|posted",
    re.I,
)
_DOM_DATE_RE = re.compile(
    r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?"
    r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?",
    re.I,
)
_SECURITY_CHALLENGE_RE = re.compile(
    r"edgeone\s*security\s*verification|security\s*verification|security\s*check|"
    r"verify\s+you(?:'|’)re\s+human|checking\s+your\s+browser|"
    r"请完成(?:安全|访问|人机)验证|正在进行(?:安全|访问)验证|"
    r"安全验证|访问验证|人机验证|安全检查",
    re.I,
)


class GenericRenderCrawler(BaseCrawler):
    MAX_PAGES = 25
    MAX_LAZY_ROUNDS = 64
    MAX_LAZY_SNAPSHOTS = 128
    MAX_LAZY_SCROLL_CONTAINERS = 32
    NAVIGATION_ATTEMPTS = 2
    JD_RAW_LIMIT = 12000
    MIN_LEN, MAX_LEN = 4, 30
    MIN_HITS = 3  # 主选择器至少命中这么多职位才认为有效
    PAGE_CHANGE_POLLS = 8
    PAGE_CHANGE_WAIT_MS = 350
    ASYNC_SETTLE_POLLS = 8
    LAZY_SIGNATURE_POLLS = 8
    MAX_NETWORK_OBSERVATIONS = 40
    MAX_CLICK_DETAILS_PER_PAGE = 2
    MAX_CLICK_DETAILS_TOTAL = 6
    CLICK_DETAIL_TIMEOUT_MS = 1200
    _API_TITLE_KEYS = (
        "jobname", "job_name", "positionname", "position_name", "postname",
        "post_name", "recruitname", "recruit_name", "title", "jobtitle", "job_title",
        "positiontitle", "position_title", "posttitle", "post_title", "name",
    )
    _API_CITY_KEYS = (
        "city", "cityname", "city_name", "location", "workplace", "work_place",
        "worklocation", "work_location", "address",
    )
    _API_DETAIL_KEYS = (
        "description", "jobdescription", "job_description", "duty", "duties",
        "requirement", "requirements", "qualification", "responsibility",
    )
    _API_URL_KEYS = (
        "detailurl", "detail_url", "joburl", "job_url", "applyurl", "apply_url", "url", "link", "href",
    )
    _API_SIGNAL_KEYS = frozenset(
        {
            "jobid", "job_id", "positionid", "position_id", "postid", "post_id",
            "department", "departmentname", "category", "jobtype", "job_type",
            "id", "url", "link", "href", "address",
            *_API_CITY_KEYS, *_API_DETAIL_KEYS,
        }
    )

    def __init__(self, company_name: str, careers_url: str):
        super().__init__(company_name, careers_url)
        self.resolved_source_url = careers_url
        self.fetch_failed = False
        self.pagination_complete: bool | None = None
        self.pages_seen = 0
        self.total_pages = None
        self.has_more = False
        self.advertised_total = None
        self.pagination_termination_reason = "not_started"
        self.pagination_diagnostics: list[dict] = []
        self.crawl_error_code = ""
        self.crawl_error_message = ""
        self._click_details_attempted = 0
        self._crawl_deadline: float | None = None
        self.recruitment_scope_changed = False
        self.category_scope_complete: bool | None = None
        self.categories_seen: list[str] = []
        self._active_category_scope = ""

    def _remaining_ms(self, fallback: int) -> int:
        if self._crawl_deadline is None:
            return max(1, fallback)
        return max(
            1,
            min(fallback, int((self._crawl_deadline - time.monotonic()) * 1_000)),
        )

    def _list_url(self) -> str:
        return self.resolved_source_url

    @staticmethod
    def _clean_source_id(value: object) -> str:
        text = str(value or "").strip()
        if not text or _PLACEHOLDER_SOURCE_ID_RE.fullmatch(text):
            return ""
        return text[:200]

    @classmethod
    def _url_has_placeholder_id(cls, url: str) -> bool:
        parsed = urlsplit(str(url or ""))
        queries = [parsed.query]
        if "?" in parsed.fragment:
            queries.append(parsed.fragment.partition("?")[2])
        for query in queries:
            for name, value in parse_qsl(query, keep_blank_values=True):
                if name.casefold() in _SOURCE_ID_KEYS and value.strip() and not cls._clean_source_id(value):
                    return True
        route = f"{parsed.path}/{parsed.fragment}"
        parts = [unquote(part) for part in re.split(r"[/#?&=]+", route) if part]
        detail_segments = {
            "job", "jobs", "position", "positions", "post", "posts",
            "detail", "details", "vacancy", "opening", "recruitment",
        }
        for index, part in enumerate(parts[:-1]):
            if part.casefold() in detail_segments and _PLACEHOLDER_SOURCE_ID_RE.fullmatch(parts[index + 1]):
                return True
        return False

    @staticmethod
    def _strip_dom_badges(text: str) -> str:
        badge = (
            r"(?:急聘|急招|急|hot|urgent|面议|薪资面议|"
            r"salary\s*negotiable|negotiable)"
        )
        cleaned = str(text or "").strip()
        for _ in range(3):
            before = cleaned
            cleaned = re.sub(
                rf"^\s*{badge}(?:\s*[-|｜:：·•]\s*|\s+)?",
                "",
                cleaned,
                flags=re.I,
            )
            cleaned = re.sub(
                rf"(?:\s*[-|｜:：·•]\s*|\s+)?{badge}\s*$",
                "",
                cleaned,
                flags=re.I,
            )
            if cleaned == before:
                break
        return cleaned.strip()

    def _clean_title(self, text: str, *, require_job_keyword: bool = False) -> str:
        """Normalize a discovered title without deciding business eligibility.

        A real employer may use a noun-only title such as ``生产管理`` or
        ``出口操作员``.  Direction matching happens after capture, so the list
        parser must not discard such rows merely because they lack a role word.
        ``require_job_keyword`` remains available for callers that explicitly
        need the legacy narrow predicate, but discovery paths leave it false.
        """
        text = re.sub(r"\s+", " ", text or "").strip()
        text = self._strip_dom_badges(text)
        text = re.sub(r"\s*(立即投递|申请职位|投递简历)\s*$", "", text).strip()
        if "个岗位" in text or text.endswith("类"):
            return ""
        if re.fullmatch(r"developers?", text, re.I):
            return ""
        if not (self.MIN_LEN <= len(text) <= self.MAX_LEN):
            return ""
        if (
            _BAD_TITLE_RE.search(text)
            or _NOISE.search(text)
            or _NON_JOB_TITLE.search(text)
            or (require_job_keyword and not _JOB_KW.search(text))
        ):
            return ""
        return text

    @staticmethod
    def _extract_city(text: str) -> str:
        matches = [match.group(1) for match in _CITY_RE.finditer(text or "")]
        return "、".join(dict.fromkeys(matches))[:40]

    @classmethod
    def _extract_job_city(cls, title: str, context: str) -> str:
        title_city = cls._extract_city(title)
        if title_city:
            return title_city
        location = re.search(r"工作地点.{0,40}", context or "")
        if location:
            cities = cls._extract_city(location.group(0))
            if cities:
                return cities.split("、", 1)[0]
        cities = cls._extract_city((context or "")[:120])
        return cities.split("、", 1)[0] if cities else ""

    @classmethod
    def _extract_dom_labels(cls, el) -> dict[str, object]:
        """Keep card badges separate from the title and inline job text."""

        scope = cls._bounded_card_scope(el) or el
        nodes = [scope, *scope.find_all(True)] if hasattr(scope, "find_all") else [scope]
        urgent = False
        negotiable = False
        published_at = ""
        for node in nodes:
            attrs = getattr(node, "attrs", {}) or {}
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            attr_text = " ".join(
                f"{key}:{attrs.get(key) or ''}"
                for key in ("class", "id", "title", "aria-label", "data-label", "data-publish-time")
            )
            compact_text = text.strip("[]【】()（） ")
            if _DOM_URGENT_RE.fullmatch(compact_text):
                urgent = True
            if _DOM_NEGOTIABLE_RE.fullmatch(compact_text):
                negotiable = True
            urgent = urgent or bool(re.search(r"(?:^|[-_\s])(urgent|hot)(?:$|[-_\s])", attr_text, re.I))
            negotiable = negotiable or bool(
                re.search(r"(?:^|[-_\s])(?:negotiable|salary-negotiable)(?:$|[-_\s])", attr_text, re.I)
            )
            if not published_at:
                date_match = _DOM_DATE_RE.search(text) or _DOM_DATE_RE.search(attr_text)
                publish_hint = _DOM_PUBLISH_HINT_RE.search(text) or _DOM_PUBLISH_HINT_RE.search(attr_text)
                date_hint = re.search(r"(?:^|[-_\s])(?:date|time|publish|posted)(?:$|[-_\s])", attr_text, re.I)
                if date_match and (publish_hint or date_hint):
                    published_at = date_match.group(0).strip()[:40]
        return {
            "urgent": bool(urgent),
            "negotiable": bool(negotiable),
            "published_at": published_at,
        }

    @classmethod
    def _is_card_boundary(cls, node) -> bool:
        name = str(getattr(node, "name", "") or "").casefold()
        if name in {"tr", "article", "li"}:
            return True
        class_tokens = {
            str(value).casefold()
            for value in (getattr(node, "get", lambda *_: [])("class") or [])
        }
        classes = " ".join(class_tokens)
        node_id = str(getattr(node, "get", lambda *_: "")("id") or "")
        if class_tokens.intersection({"card", "item", "items"}) or _CARD_CLASS.search(
            f"{classes} {node_id}"
        ):
            return True
        attrs = getattr(node, "attrs", {}) or {}
        return any(
            str(key).casefold() in {
                "data-job-id", "data-position-id", "data-post-id", "data-pid",
                "jobid", "job_id", "positionid", "position_id", "postid", "post_id", "pid",
            }
            and cls._clean_source_id(value)
            for key, value in attrs.items()
        )

    @classmethod
    def _card_context(cls, el) -> str:
        """Stop at one job row/card; never promote a title into the surrounding list."""
        context = el.get_text(" ", strip=True)
        parent = getattr(el, "parent", None)
        while parent:
            candidate = parent.get_text(" ", strip=True)
            if cls._is_card_boundary(parent):
                return candidate
            if len(candidate) > 600 or str(getattr(parent, "name", "")).casefold() in {
                "table", "tbody", "thead", "tfoot", "ul", "ol", "section", "main", "body",
            }:
                break
            context = candidate
            parent = getattr(parent, "parent", None)
        return context

    @staticmethod
    def _normalize_scope_url(url: str, *, strip_paging: bool) -> str:
        parsed = urlsplit(str(url or ""))
        query: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.casefold()
            if lowered in _VOLATILE_QUERY_KEYS or (strip_paging and lowered in _PAGING_QUERY_KEYS):
                continue
            query.append((key, value))
        query.sort()
        fragment = parsed.fragment
        if strip_paging and "?" in fragment:
            fragment_path, _, fragment_query = fragment.partition("?")
            fragment_items = [
                (key, value)
                for key, value in parse_qsl(fragment_query, keep_blank_values=True)
                if key.casefold() not in _VOLATILE_QUERY_KEYS
                and key.casefold() not in _PAGING_QUERY_KEYS
            ]
            fragment_items.sort()
            fragment = fragment_path
            if fragment_items:
                fragment = f"{fragment}?{urlencode(fragment_items)}"
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, urlencode(query), fragment))

    @classmethod
    def _listing_scope_key(cls, url: str) -> str:
        return cls._normalize_scope_url(url, strip_paging=True)

    @classmethod
    def _is_post_scope_volatile_key(cls, key: object) -> bool:
        compact = re.sub(r"[^a-z0-9]", "", str(key or "").casefold())
        return (
            compact in {
                "page", "p", "pageindex", "pageno", "pagenum", "currentpage",
                "offset", "start", "limit", "pagesize", "cursor", "nextcursor",
                "prevcursor", "secret", "secretkey", "secrettoken",
            }
            or compact.startswith("page")
            or compact.startswith("cursor")
            or compact.endswith("cursor")
            or compact.startswith("secret")
        )

    @classmethod
    def _strip_post_scope_volatile_fields(cls, value):
        if isinstance(value, dict):
            return {
                str(key): cls._strip_post_scope_volatile_fields(item)
                for key, item in value.items()
                if not cls._is_post_scope_volatile_key(key)
            }
        if isinstance(value, list):
            return [cls._strip_post_scope_volatile_fields(item) for item in value]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        raise TypeError(f"unsupported POST scope value: {type(value).__name__}")

    @classmethod
    def _post_scope_hash(cls, post_data_json) -> str | None:
        try:
            normalized = cls._strip_post_scope_volatile_fields(post_data_json)
            encoded = json.dumps(
                normalized,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _api_scope_key(
        cls,
        url: str,
        post_data_json=_POST_DATA_UNSET,
        *,
        post_data_parsed: bool = True,
        unparsed_marker: object = None,
    ) -> str:
        scope = cls._normalize_scope_url(url, strip_paging=True)
        if post_data_json is _POST_DATA_UNSET:
            return scope
        if post_data_parsed:
            post_hash = cls._post_scope_hash(post_data_json)
        else:
            post_hash = None
        if post_hash is None:
            marker = unparsed_marker if unparsed_marker is not None else id(post_data_json)
            post_hash = hashlib.sha256(f"unparsed:{marker!r}".encode("utf-8")).hexdigest()[:16]
        return f"{scope}#post:{post_hash}"

    @classmethod
    def _scope_hash(cls, url: str) -> str:
        return hashlib.sha256(cls._listing_scope_key(url).encode("utf-8")).hexdigest()[:12]

    def _record_pagination_diagnostic(
        self,
        *,
        page: int,
        count: int,
        total: int | None,
        reason: str,
        changed: bool,
        source_url: str,
    ) -> None:
        if len(self.pagination_diagnostics) >= 30:
            return
        self.pagination_diagnostics.append(
            {
                "page": max(0, min(int(page), self.MAX_PAGES)),
                "count": max(0, min(int(count), 100000)),
                "total": total if total is None else max(0, min(int(total), 100000)),
                "reason": str(reason or "")[:80],
                "changed": bool(changed),
                "scope_hash": self._scope_hash(source_url),
            }
        )

    @staticmethod
    def _visible_locator_exists(page, selector: str) -> bool:
        try:
            locator = page.locator(selector)
            count = min(int(locator.count()), 8)
            for index in range(count):
                item = locator.nth(index)
                if item.is_visible():
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def _html_node_visible(node) -> bool:
        if node.has_attr("hidden"):
            return False
        style = str(node.get("style") or "").casefold().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style or "opacity:0" in style:
            return False
        classes = " ".join(str(value) for value in (node.get("class") or [])).casefold()
        return not any(token in classes for token in ("hidden", "d-none", "invisible"))

    @classmethod
    def _classify_access_block(cls, html: str, current_url: str = "") -> str:
        """Classify only a real visible access challenge, not a standalone login link."""
        soup = BeautifulSoup(html or "", "html.parser")
        visible_inputs = [node for node in soup.find_all("input") if cls._html_node_visible(node)]
        body_text = soup.get_text(" ", strip=True)
        title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
        login_words = re.search(r"请先登录|登录后(?:查看|访问|才能)|需要登录|手机验证码|短信验证码", body_text, re.I)
        has_password = any(
            str(node.get("type") or "").casefold() == "password"
            or re.search(r"password|密码", " ".join(str(node.get(key) or "") for key in ("name", "id", "placeholder")), re.I)
            for node in visible_inputs
        )
        has_phone = any(
            str(node.get("type") or "").casefold() in {"tel", "number"}
            or re.search(r"phone|mobile|手机号|手机", " ".join(str(node.get(key) or "") for key in ("name", "id", "placeholder")), re.I)
            for node in visible_inputs
        )
        has_otp = any(
            re.search(r"验证码|verification|otp", " ".join(str(node.get(key) or "") for key in ("name", "id", "placeholder")), re.I)
            for node in visible_inputs
        )
        has_login_button = any(
            re.search(r"登录|获取验证码|短信验证|sign\s*in", node.get_text(" ", strip=True), re.I)
            for node in soup.find_all(["button", "input"])
            if cls._html_node_visible(node)
        )
        path = urlsplit(current_url).path.casefold()
        login_form = (has_password or (has_phone and has_otp)) and (login_words or has_login_button)
        if login_form or (path.endswith(("/login", "/signin", "/auth")) and (has_password or has_phone)):
            return "login_required"

        # Edge security pages often contain no input, form, or captcha class at
        # all.  Treat the page-level challenge as blocked only when the page
        # does not also expose job-shaped markup; a security phrase inside a
        # real JD is not an access decision.
        challenge_text = " ".join(
            part for part in (title_text, body_text[:1600], path) if part
        )
        has_job_markup = bool(
            soup.find(attrs={"data-job-id": True})
            or soup.find(attrs={"data-position-id": True})
            or soup.find(attrs={"data-post-id": True})
            or soup.find(class_=_CARD_CLASS)
            or soup.find(class_=re.compile(r"(?:job|position|post|vacancy|opening)[-_]?(?:card|item|row)", re.I))
        )
        if _SECURITY_CHALLENGE_RE.search(challenge_text) and not has_job_markup:
            return "accessblocked"

        captcha_controls = [
            node for node in soup.find_all(["iframe", "input", "div", "form"])
            if cls._html_node_visible(node)
            and re.search(r"captcha|challenge|人机|滑动验证|图形验证码", " ".join(
                [str(node.name or ""), str(node.get("id") or ""), " ".join(str(value) for value in (node.get("class") or [])),
                 str(node.get("src") or ""), str(node.get("action") or ""), str(node.get("placeholder") or "")]
            ), re.I)
        ]
        if captcha_controls:
            return "captcha_required"
        return ""

    def _access_block_code(self, page, html: str | None = None) -> str:
        content = html if html is not None else page.content()
        code = self._classify_access_block(content, str(getattr(page, "url", "") or ""))
        if code:
            return code
        current_url = str(getattr(page, "url", "") or "")
        if re.search(r"edgeone|challenge[-_]?platform|security[-_]?(?:check|verification)", current_url, re.I):
            return "accessblocked"
        # DOM visibility is more reliable than static HTML for hidden challenge widgets.
        if self._visible_locator_exists(page, "iframe[src*='captcha'], [id*='captcha'], [class*='captcha'], [id*='challenge'], [class*='challenge']"):
            return "captcha_required"
        if self._visible_locator_exists(page, "input[type='password']"):
            visible_login_text = self._visible_locator_exists(page, "button, input[type='submit']")
            if visible_login_text:
                return "login_required"
        return ""

    def _set_access_block(self, code: str) -> None:
        self.crawl_error_code = code
        self.pagination_complete = False
        self.crawl_error_message = (
            "招聘列表需要登录后访问"
            if code == "login_required"
            else "招聘列表被安全验证阻断"
            if code == "accessblocked"
            else "招聘列表出现验证码挑战"
        )
        self.pagination_termination_reason = code

    def _dismiss_recruitment_notice(self, page) -> bool:
        """Dismiss ordinary recruitment notices only; never click application controls."""
        pattern = re.compile(r"^(?:我已阅读并同意|我已阅读|我知道了|知道了|关闭公告|关闭通知|同意并继续)$")
        for getter in (
            lambda: page.get_by_role("button", name=pattern).first,
            lambda: page.get_by_text(pattern).first,
        ):
            try:
                target = getter()
                if target.count() and target.is_visible():
                    target.click(timeout=2500)
                    page.wait_for_timeout(self._remaining_ms(250))
                    return True
            except Exception:
                continue
        return False

    def _submit_empty_search(self, page) -> bool:
        """Run a blank recruitment search when a site hides its list until submit."""
        try:
            inputs = page.locator(
                "input[placeholder*='搜索'], input[placeholder*='查询'], input[placeholder*='关键词'], "
                "input[aria-label*='搜索'], input[aria-label*='查询'], input[name*='search'], input[name*='keyword']"
            )
            count = min(int(inputs.count()), 5)
        except Exception:
            return False
        for index in range(count):
            try:
                field = inputs.nth(index)
                if not field.is_visible():
                    continue
                field.fill("")
                for getter in (
                    lambda: page.get_by_role("button", name=re.compile(r"^(?:搜索|查询)$")).first,
                    lambda: page.get_by_text(re.compile(r"^(?:搜索|查询)$")).first,
                ):
                    try:
                        button = getter()
                        if button.count() and button.is_visible():
                            button.click(timeout=2500)
                            self._wait_for_async_list(page)
                            return True
                    except Exception:
                        continue
                field.press("Enter")
                self._wait_for_async_list(page)
                return True
            except Exception:
                continue
        return False

    def _run_observed_entry_interaction(self, page) -> bool:
        """Perform bounded safe entry actions without stopping after a notice dismissal."""
        dismissed = self._dismiss_recruitment_notice(page)
        campus_clicked = self._click_campus_entry(page)
        searched = self._submit_empty_search(page)
        return dismissed or searched or campus_clicked

    def _wait_for_async_list(self, page) -> bool:
        """Wait for loading masks to clear; a transient zero is never a terminal result."""
        waited = False
        quiet_rounds = 0
        for _ in range(self.ASYNC_SETTLE_POLLS):
            if self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline:
                break
            loading = any(self._visible_locator_exists(page, selector) for selector in _LOADING_SELECTORS)
            if loading:
                waited = True
                quiet_rounds = 0
                try:
                    page.wait_for_timeout(self._remaining_ms(self.PAGE_CHANGE_WAIT_MS))
                except Exception:
                    break
                continue
            quiet_rounds += 1
            if quiet_rounds >= 2:
                break
            try:
                page.wait_for_timeout(self._remaining_ms(self.PAGE_CHANGE_WAIT_MS))
            except Exception:
                break
        return waited

    @staticmethod
    def _current_page_number(html: str) -> int | None:
        soup = BeautifulSoup(html or "", "html.parser")
        candidates = soup.select(
            "[aria-current='page'], .active, .current, [class*='active'], [class*='current']"
        )
        for node in candidates:
            text = node.get_text(" ", strip=True)
            match = re.fullmatch(r"\d{1,5}", text)
            if match:
                return int(match.group(0))
        return None

    def _page_snapshot(self, html: str, sig: str, current_url: str) -> tuple[int | None, str, str]:
        soup = BeautifulSoup(html or "", "html.parser")
        items: list[str] = []
        if sig:
            for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
                if self._sig(el) != sig:
                    continue
                title = self._clean_title(self._direct_text(el))
                if title:
                    href, _, observed = self._job_link_evidence(el, current_url)
                    context = self._card_context(el)[:500]
                    native_id = self._native_job_id(el)
                    items.append(f"{native_id}\x1f{title}\x1f{href if observed else context}")
        else:
            items = [
                "\x1f".join(key)
                for key in sorted(self._lazy_dom_job_keys(html, current_url))
            ]
            if not items:
                line_jobs = self._parse_line_jobs(html)
                items = sorted(
                    f"{job.get('title', '')}\x1f{job.get('city', '')}"
                    for job in line_jobs
                )
            if not items:
                body = soup.body or soup
                for node in body.find_all(["script", "style", "noscript"]):
                    node.decompose()
                text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))[:2000]
                items = [text] if text else []
        content_key = f"{len(items)}:{hashlib.sha256(chr(30).join(items).encode('utf-8')).hexdigest()[:16]}"
        return self._current_page_number(html), content_key, self._normalize_scope_url(current_url, strip_paging=False)

    @staticmethod
    def _snapshot_changed(before: tuple[int | None, str, str], after: tuple[int | None, str, str]) -> bool:
        # Page number/URL can update before the SPA has replaced its old rows.
        return before[1] != after[1]

    def _find_next_control(self, page):
        selector = (
            ".el-pagination .btn-next, .ant-pagination-next, .pagination .btn-next, "
            "[class*='pagination'] [class*='next'], [class*='pagination'] li[title='下一页'], "
            "[class*='pagination'] a[aria-label='Next'], [class*='pagination'] button[aria-label='Next'], "
            "[class*='pagination'] [rel='next'], [class*='pagination'] [title='下一页'], "
            "[class*='pagination'] [aria-label='下一页'], nav[aria-label*='分页'] [rel='next']"
            ", button.next, a.next, [class*='next-page'], [class*='nextPage'], "
            "[data-page='next'], [data-action='next']"
        )
        try:
            locator = page.locator(selector)
            for index in range(min(int(locator.count()), 20)):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                try:
                    owner = candidate.locator(
                        "xpath=ancestor::*[contains(translate(@class, 'PAGINATION', 'pagination'), 'pagination') "
                        "or @role='navigation' or contains(@aria-label, '分页')][1]"
                    )
                    if owner.count() == 0:
                        continue
                except Exception:
                    # The selector is already scoped; old Playwright doubles may not expose ancestor locators.
                    pass
                return candidate
        except Exception:
            pass
        for getter in (
            lambda: page.get_by_role("button", name=re.compile(r"^\s*(?:下一页|next)\s*$", re.I)).first,
            lambda: page.get_by_text(re.compile(r"^\s*(?:下一页|next)\s*$", re.I)).first,
        ):
            try:
                candidate = getter()
                if candidate.count() and candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _next_is_disabled(locator) -> bool:
        if locator is None:
            return True
        try:
            if hasattr(locator, "is_disabled") and locator.is_disabled():
                return True
        except Exception:
            pass
        try:
            disabled_attr = locator.get_attribute("disabled")
            attributes = {
                "class": locator.get_attribute("class") or "",
                "aria-disabled": locator.get_attribute("aria-disabled") or "",
                "disabled": disabled_attr,
                "data-disabled": locator.get_attribute("data-disabled") or "",
            }
        except Exception:
            return False
        return (
            "disabled" in attributes["class"].casefold()
            or attributes["aria-disabled"].casefold() == "true"
            or attributes["data-disabled"].casefold() in {"true", "1", "disabled"}
            or attributes["disabled"] is not None
        )

    def _wait_for_page_change(
        self,
        page,
        before: tuple[int | None, str, str],
        selector_sig: str,
    ) -> bool:
        changed_key = None
        for _ in range(self.PAGE_CHANGE_POLLS):
            if self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline:
                break
            try:
                page.wait_for_timeout(self._remaining_ms(self.PAGE_CHANGE_WAIT_MS))
                current = self._page_snapshot(page.content(), selector_sig, str(getattr(page, "url", "") or ""))
            except Exception:
                continue
            if self._snapshot_changed(before, current):
                if current[1].startswith("0:") and not before[1].startswith("0:"):
                    # A cleared list is commonly the SPA loading state, not a new page.
                    continue
                if current[1] == changed_key:
                    self._wait_for_async_list(page)
                    return True
                changed_key = current[1]
        return False

    @classmethod
    def _job_identity_key(cls, job: dict) -> tuple[str, ...]:
        for field in ("native_job_id", "source_job_id"):
            value = str(job.get(field) or "").strip().casefold()
            if value:
                return (field, value)
        if job.get("detail_link_observed") or job.get("detail_link_api_observed") or job.get("detail_link_click_observed"):
            url = str(job.get("jd_url") or "").strip().casefold()
            if url:
                return ("detail_url", url)
        base = cls._job_base_key(job)
        return ("title_city", *base)

    def _lazy_dom_job_keys(self, html: str, source_url: str) -> set[tuple[str, ...]]:
        soup = BeautifulSoup(html or "", "html.parser")
        keys: set[tuple[str, ...]] = set()
        for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            title, context, detail_url, valid = self._is_real_dom_candidate(el, source_url)
            if not valid:
                continue
            native_id = self._native_job_id(el)
            if native_id:
                keys.add(("native_job_id", native_id.casefold()))
                continue
            if detail_url and self._valid_observed_detail_url(detail_url, source_url):
                keys.add(("detail_url", detail_url.casefold()))
                continue
            city = self._extract_job_city(title, context)
            keys.add(("title_city", title.casefold(), city.casefold()))
        return keys

    def _lazy_list_signature(self, html: str, source_url: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        items = sorted(
            "\x1f".join(key)
            for key in self._lazy_dom_job_keys(html, source_url)
        )
        controls = []
        for node in soup.find_all(["button", "a", "input", "[role='button']"]):
            text = node.get_text(" ", strip=True) or str(node.get("value") or "").strip()
            signal = " ".join(
                [
                    text,
                    str(node.get("id") or ""),
                    " ".join(str(value) for value in (node.get("class") or [])),
                ]
            )
            if re.search(
                r"加载更多|更多职位|显示更多|load[-_\s]+more|show[-_\s]+more|"
                r"more[-_\s]+(?:jobs?|positions?)",
                signal,
                re.I,
            ):
                controls.append(
                    f"{signal}\x1f{node.get('disabled')}\x1f{node.get('aria-disabled')}"
                )
        payload = "\x1e".join([*items, *sorted(controls)])
        return f"{len(items)}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    def _advance_lazy_scroll(self, page) -> bool:
        try:
            result = page.evaluate(
                """/* recruitops-lazy-scroll */
                () => {
                    const root = document.scrollingElement;
                    const nodes = [root, ...document.querySelectorAll('*')].filter(Boolean);
                    const visibleScrollable = node => {
                        if (node === root) return true;
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        const visible = style.display !== 'none' && style.visibility !== 'hidden'
                            && rect.width > 0 && rect.height > 0;
                        return visible && /(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`);
                    };
                    const scrollable = [];
                    for (const node of nodes.slice(0, 4096)) {
                        const max = node.scrollHeight - node.clientHeight;
                        if (visibleScrollable(node) && max > node.scrollTop + 2) {
                            scrollable.push(node);
                        }
                        if (scrollable.length >= 32) break;
                    }
                    let moved = 0;
                    for (const node of scrollable) {
                        const max = node.scrollHeight - node.clientHeight;
                        const before = node.scrollTop;
                        const step = Math.max(1, Math.floor(node.clientHeight * 0.8));
                        node.scrollTop = Math.min(max, before + step);
                        if (node.scrollTop > before + 2) moved += 1;
                    }
                    return moved;
                }"""
            )
            if isinstance(result, dict):
                return bool(result.get("moved"))
            return bool(result)
        except Exception:
            return False

    def _load_more_state(self, page) -> dict[str, bool]:
        try:
            result = page.evaluate(
                """/* recruitops-load-more */
                () => {
                    const pattern = /^(?:加载更多|更多职位|显示更多(?:职位)?|更多|load[-_\\s]+more|show[-_\\s]+more|more[-_\\s]+(?:jobs?|positions?))$/i;
                    const visible = node => {
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const nodes = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')];
                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || node.value || '').replace(/\\s+/g, ' ').trim();
                        const signal = `${text} ${node.id || ''} ${node.className || ''}`.replace(/\\s+/g, ' ').trim();
                        if (!(pattern.test(text) || /load[-_\\s]?more|more[-_\\s]?(?:jobs?|positions?)/i.test(signal)) || !visible(node)) continue;
                        const disabled = node.disabled || node.getAttribute('aria-disabled') === 'true' || /disabled/.test(node.className || '');
                        if (disabled) return {found: true, clicked: false, disabled: true};
                        node.click();
                        return {found: true, clicked: true, disabled: false};
                    }
                    return {found: false, clicked: false, disabled: false};
                }"""
            )
            if isinstance(result, dict):
                return {key: bool(result.get(key)) for key in ("found", "clicked", "disabled")}
        except Exception:
            pass
        return {"found": False, "clicked": False, "disabled": False}

    def _wait_for_lazy_signature(self, page, before: str, source_url: str) -> tuple[bool, str]:
        current = before
        for _ in range(self.LAZY_SIGNATURE_POLLS):
            if self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline:
                break
            try:
                page.wait_for_timeout(self._remaining_ms(self.PAGE_CHANGE_WAIT_MS))
                html = page.content()
                current = self._lazy_list_signature(html, source_url)
            except Exception:
                continue
            if current != before:
                return True, current
        return False, current

    @classmethod
    def _inline_jd(cls, context: str, title: str = "") -> str:
        text = re.sub(r"\s+", " ", context or "").strip()
        if not text or not _JD_SIGNAL.search(text):
            return ""
        return text[: cls.JD_RAW_LIMIT]

    @staticmethod
    def _anchor_is_apply(anchor) -> bool:
        text = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "")
        return bool(_APPLY_TEXT.search(f"{text} {href}"))

    @staticmethod
    def _anchor_is_non_job_navigation(anchor, resolved_url: str) -> bool:
        """Reject company navigation links that look like detail links only by shape."""

        text = anchor.get_text(" ", strip=True)
        parsed = urlsplit(resolved_url)
        route = f"{parsed.path}/{parsed.fragment}"
        return bool(_NON_JOB_TITLE.search(text) or _NON_JOB_ROUTE.search(route))

    @classmethod
    def _anchor_is_pagination(cls, anchor, source_url: str) -> bool:
        href = str(anchor.get("href") or "").strip()
        resolved = urljoin(source_url, href)
        parsed = urlsplit(resolved)
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        detail_query_keys = {
            "id", "jobid", "job_id", "jobadid", "job_ad_id", "positionid", "position_id",
            "postid", "post_id", "pid", "recruitmentid", "recruitment_id",
        }
        if any(
            key.casefold() in _PAGING_QUERY_KEYS
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ) and not query_keys.intersection(detail_query_keys):
            return True
        node = anchor
        while node:
            attrs = getattr(node, "attrs", {}) or {}
            signal = " ".join(
                [
                    str(node.get("id") or ""),
                    " ".join(str(value) for value in (node.get("class") or [])),
                    str(node.get("rel") or ""),
                    str(node.get("title") or ""),
                    str(node.get("aria-label") or ""),
                ]
            )
            if re.search(
                r"pagination|pager|page[-_ ]?(?:item|link|next|prev)|btn[-_ ]?(?:next|prev)|"
                r"(?:^|\s)(?:next|prev|previous|下一页|上一页|分页|页码)(?:\s|$)",
                signal,
                re.I,
            ):
                return True
            if str(getattr(node, "name", "")).casefold() in {"ul", "ol", "nav", "section", "main", "body"}:
                break
            node = getattr(node, "parent", None)
        return False

    @classmethod
    def _bounded_card_scope(cls, el):
        node = el
        while node:
            if cls._is_card_boundary(node):
                return node
            if str(getattr(node, "name", "")).casefold() in {
                "table", "tbody", "thead", "tfoot", "ul", "ol", "section", "main", "body",
            }:
                return None
            node = getattr(node, "parent", None)
        return None

    @classmethod
    def _native_job_id(cls, el) -> str:
        scope = cls._bounded_card_scope(el)
        if scope is None:
            return ""
        id_keys = {
            "data-jobid", "data-job-id", "data-position-id", "data-post-id", "data-pid",
            "jobid", "job_id", "positionid", "position_id", "postid", "post_id", "pid",
        }
        for node in [scope, *scope.find_all(True)]:
            for key, value in (getattr(node, "attrs", {}) or {}).items():
                if str(key).casefold() in id_keys:
                    normalized = cls._clean_source_id(value)
                    if normalized:
                        return normalized
        return ""

    @classmethod
    def _observed_job_id_from_url(cls, url: str) -> str:
        parsed = urlsplit(str(url or ""))
        for query in (parsed.query, parsed.fragment.partition("?")[2]):
            for name, value in parse_qsl(query):
                if name.casefold() in _SOURCE_ID_KEYS:
                    normalized = cls._clean_source_id(value)
                    if normalized:
                        return normalized
        return ""

    def _job_link_observation(
        self,
        el,
        base_url: str | None = None,
    ) -> tuple[str, str, bool, str]:
        """Read one bounded HTTP href and retain the raw DOM value."""

        source_url = str(base_url or self._list_url() or "")
        scope = self._bounded_card_scope(el)
        if scope is None:
            return source_url, "list", False, ""
        own_anchor = el if getattr(el, "name", "") == "a" else el.find_parent("a", href=True)
        anchors = [own_anchor] if own_anchor is not None else (
            [scope] if getattr(scope, "name", "") == "a" else scope.find_all("a", href=True)
        )
        for anchor in anchors:
            if self._anchor_is_apply(anchor) or self._anchor_is_pagination(anchor, source_url):
                continue
            href = str(anchor.get("href") or "").strip()
            if not href or href.casefold().startswith(("javascript:", "mailto:", "tel:")):
                continue
            resolved = urljoin(source_url, href)
            parsed = urlsplit(resolved)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            if self._anchor_is_non_job_navigation(anchor, resolved):
                continue
            if not self._valid_observed_detail_url(resolved, source_url):
                continue
            return resolved, "detail", True, href
        return source_url, "list", False, ""

    def _job_link_evidence(self, el, base_url: str | None = None) -> tuple[str, str, bool]:
        """Read only an HTTP href from the bounded job card; never synthesize detail URLs."""

        detail_url, link_kind, observed, _ = self._job_link_observation(el, base_url)
        return detail_url, link_kind, observed

    def _job_link(self, el) -> tuple[str, str]:
        url, link_kind, _ = self._job_link_evidence(el)
        return url, link_kind

    @classmethod
    def _has_click_detail_hint(cls, el) -> bool:
        node = el
        while node:
            attrs = getattr(node, "attrs", {}) or {}
            classes = " ".join(str(value) for value in (attrs.get("class") or []))
            if _CARD_CLASS.search(classes):
                return True
            if any(
                str(key).casefold() in {
                    "pid", "positionid", "position_id", "jobid", "job_id", "postid", "post_id",
                    "data-pid", "data-position-id", "data-job-id", "data-post-id", "projectid", "recruitment",
                }
                and cls._clean_source_id(value)
                for key, value in attrs.items()
            ):
                return True
            if str(getattr(node, "name", "")).casefold() in {"tr", "article", "li"}:
                return False
            node = getattr(node, "parent", None)
        return False

    def _is_real_dom_candidate(self, el, base_url: str | None = None) -> tuple[str, str, str, bool]:
        for parent in (el, *el.parents):
            if getattr(parent, "name", "") in {"header", "footer", "nav"} or parent.get("role") == "navigation":
                return "", "", "", False
            if re.search(r"^(?:header|footer|nav|menu)(?:$|[-_])", str(parent.get("id") or ""), re.I):
                return "", "", "", False
        title = self._clean_title(self._direct_text(el))
        if not title:
            return "", "", "", False
        if _NON_JOB_TITLE.fullmatch(title):
            return "", "", "", False
        context = self._card_context(el)
        inline_jd = self._inline_jd(context, title)
        if not inline_jd and _CONTEXT_NOISE.search(context):
            return "", "", "", False
        detail_url, link_kind, observed = self._job_link_evidence(el, base_url)
        bounded = self._is_bounded_job_card(el)
        route_hint = bool(_DETAIL_ROUTE.search(detail_url)) if observed else False
        title_class = " ".join(
            str(value)
            for value in (getattr(el, "get", lambda *_: [])("class") or [])
        )
        structured_title = bool(re.search(r"(?:title|name)", title_class, re.I))
        title_element = structured_title or getattr(el, "name", "") in {"h2", "h3", "h4", "h5", "a"}
        if not title_element and not _JOB_KW.search(title):
            return "", "", "", False
        evidence = bool(
            inline_jd
            or _JOB_CONTEXT_SIGNAL.search(context)
            or (
                bounded
                and (
                    observed
                    or route_hint
                    or self._card_has_job_class(el)
                    or structured_title
                    or getattr(el, "name", "") == "tr"
                )
            )
        )
        return title, context, detail_url, bool(evidence and (observed or bounded))

    @classmethod
    def _is_bounded_job_card(cls, el) -> bool:
        return cls._bounded_card_scope(el) is not None

    @classmethod
    def _card_has_job_class(cls, el) -> bool:
        node = el
        while node:
            attrs = getattr(node, "attrs", {}) or {}
            value = " ".join(str(item) for item in (attrs.get("class") or []))
            if re.search(r"job|position|post|vacancy|opening|recruit", value, re.I):
                return True
            if cls._is_card_boundary(node):
                return False
            node = getattr(node, "parent", None)
        return False

    @classmethod
    def _valid_observed_detail_url(cls, url: str, source_url: str) -> bool:
        candidate = str(url or "").strip()
        source = str(source_url or "").strip()
        parsed = urlsplit(candidate)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and candidate != source
            and parsed.netloc.casefold() not in _NON_JOB_HOSTS
            and not cls._url_has_placeholder_id(candidate)
            and cls._listing_scope_key(candidate) != cls._listing_scope_key(source)
        )

    @classmethod
    def _card_locator(cls, el, selector_sig: str, index: int) -> dict[str, object]:
        scope = cls._bounded_card_scope(el)
        locator: dict[str, object] = {
            "selector": str(selector_sig or ""),
            "index": int(index),
        }
        if scope is None:
            return locator
        classes = " ".join(str(value) for value in (scope.get("class") or []))
        locator["card_tag"] = str(getattr(scope, "name", "") or "")
        if classes:
            locator["card_class"] = classes[:240]
        for key in (
            "id", "data-job-id", "data-jobid", "data-position-id",
            "data-post-id", "data-pid", "jobid", "job_id",
            "positionid", "position_id", "postid", "post_id", "pid",
        ):
            value = scope.get(key)
            if key == "id":
                value = str(value or "").strip()
            else:
                value = cls._clean_source_id(value)
            if value:
                locator["card_attribute"] = key
                locator["card_value"] = str(value)[:200]
                break
        if locator.get("card_tag") and (classes or locator.get("card_attribute")):
            card_selector = locator["card_tag"]
            if classes:
                card_selector += "." + ".".join(classes.split())
            locator["card_selector"] = card_selector[:500]
        return locator

    def _click_detail_at_index(self, page, selector_sig: str, index: int, source_url: str) -> str:
        try:
            title_locator = page.locator(selector_sig).nth(index)
            if not title_locator.count() or not title_locator.is_visible():
                return ""
        except Exception:
            return ""
        target = title_locator
        popup = None
        expect_popup = getattr(page, "expect_popup", None)
        if expect_popup is not None:
            try:
                with expect_popup(timeout=self.CLICK_DETAIL_TIMEOUT_MS) as popup_info:
                    target.click(timeout=self.CLICK_DETAIL_TIMEOUT_MS)
                popup = popup_info.value
            except Exception:
                popup = None
        else:
            try:
                target.click(timeout=self.CLICK_DETAIL_TIMEOUT_MS)
            except Exception:
                return ""
        if popup is not None:
            try:
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=self.CLICK_DETAIL_TIMEOUT_MS)
                except Exception:
                    pass
                detail_url = str(getattr(popup, "url", "") or "")
                return detail_url if self._valid_observed_detail_url(detail_url, source_url) else ""
            finally:
                try:
                    popup.close()
                except Exception:
                    pass
        detail_url = str(getattr(page, "url", "") or "")
        if not self._valid_observed_detail_url(detail_url, source_url):
            return ""
        try:
            page.go_back(wait_until="domcontentloaded", timeout=self.CLICK_DETAIL_TIMEOUT_MS)
            page.wait_for_timeout(self._remaining_ms(250))
        except Exception:
            return ""
        return detail_url

    def _network_detail_links(self, observations: list[dict], source_url: str) -> dict[str, tuple[str, str]]:
        """Return exact detail URLs already present in the current listing response scope."""
        source_scope = self._listing_scope_key(source_url)
        links: dict[str, tuple[str, str]] = {}
        for observation in observations:
            observed_scope = str(observation.get("source_scope") or "")
            if observed_scope != source_scope and not observed_scope.startswith(
                f"{source_scope}#category:"
            ):
                continue
            for job in observation.get("jobs") or []:
                title = str(job.get("title") or "").strip()
                detail_url = str(job.get("jd_url") or "").strip()
                if (
                    not title
                    or str(job.get("link_kind") or "").casefold() != "detail"
                    or not self._valid_observed_detail_url(detail_url, source_url)
                ):
                    continue
                links.setdefault(title, (detail_url, str(job.get("jd_raw") or "")))
        return links

    def _observe_click_detail_links(
        self,
        page,
        html: str,
        selector_sig: str,
        source_url: str,
        *,
        network_detail_links: dict[str, tuple[str, str]] | None = None,
        detail_pending: set[str] | None = None,
    ) -> dict[str, str]:
        """Observe known card/route/new-tab details with a bounded click budget."""
        soup = BeautifulSoup(html or "", "html.parser")
        observed: dict[str, str] = {}
        network_detail_links = network_detail_links or {}
        detail_pending = detail_pending if detail_pending is not None else set()
        page_attempts = 0
        index = 0
        for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            if self._sig(el) != selector_sig:
                continue
            title = self._clean_title(self._direct_text(el))
            if not title:
                index += 1
                continue
            _, _, has_href = self._job_link_evidence(el, source_url)
            if has_href or title in network_detail_links:
                index += 1
                continue
            if not self._has_click_detail_hint(el):
                detail_pending.add(title)
                index += 1
                continue
            if (
                page_attempts >= self.MAX_CLICK_DETAILS_PER_PAGE
                or self._click_details_attempted >= self.MAX_CLICK_DETAILS_TOTAL
            ):
                detail_pending.add(title)
                index += 1
                continue
            page_attempts += 1
            self._click_details_attempted += 1
            detail_url = self._click_detail_at_index(page, selector_sig, index, source_url)
            if detail_url:
                observed[title] = detail_url
            else:
                detail_pending.add(title)
            index += 1
        return observed

    def fetch(self) -> list[dict]:
        if TonghuashunCampusCrawler.supports(self.careers_url):
            adapter = TonghuashunCampusCrawler(self.company_name, self.careers_url)
            jobs = adapter.fetch()
            for name in (
                "resolved_source_url", "fetch_failed", "pagination_complete", "pages_seen",
                "total_pages", "has_more", "advertised_total", "pagination_termination_reason",
                "pagination_diagnostics", "crawl_error_code", "crawl_error_message",
            ):
                setattr(self, name, getattr(adapter, name, getattr(self, name, None)))
            return jobs

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.error("[%s] 未安装 playwright", self.company_name)
            return []

        jobs, seen = [], set()
        network_observations: list[dict] = []
        selector_sig = None
        selector_fallback = False
        page = None
        self._click_details_attempted = 0
        self._crawl_deadline = time.monotonic() + effective_crawl_timeout_seconds(180.0)
        browser = None
        ctx = None

        def close_browser() -> None:
            nonlocal browser, ctx
            if ctx is not None:
                try:
                    ctx.close()
                finally:
                    ctx = None
            if browser is not None:
                try:
                    browser.close()
                finally:
                    browser = None

        try:
            with sync_playwright() as p:
                browser = launch_browser(
                    p,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                          "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context(user_agent=_UA, viewport={"width": 1366, "height": 768},
                                          locale="zh-CN", ignore_https_errors=True)
                page = ctx.new_page()

                def collect_response(response) -> None:
                    try:
                        if len(network_observations) >= self.MAX_NETWORK_OBSERVATIONS:
                            return
                        content_type = (response.headers.get("content-type") or "").casefold()
                        # SPA list APIs often use generic paths such as /schedule or /query.
                        # The bounded job-shaped payload is the useful signal, not the URL name.
                        if "json" not in content_type:
                            return
                        extracted, totals, has_more = self._extract_api_payload(
                            response.json(), response.url
                        )
                        if not extracted:
                            return
                        request = getattr(response, "request", None)
                        request_method = str(getattr(request, "method", "GET") or "GET").upper()
                        post_data_json = _POST_DATA_UNSET
                        post_data_parsed = True
                        unparsed_marker = None
                        if request_method == "POST":
                            try:
                                post_data_json = getattr(request, "post_data_json")
                            except Exception:
                                post_data_parsed = False
                                unparsed_marker = id(response)
                        source_scope = self._listing_scope_key(
                            str(getattr(page, "url", "") or self.resolved_source_url)
                        )
                        if self._active_category_scope:
                            source_scope = f"{source_scope}#category:{self._active_category_scope}"
                        network_observations.append(
                            {
                                "source_scope": source_scope,
                                "api_scope": self._api_scope_key(
                                    response.url,
                                    post_data_json,
                                    post_data_parsed=post_data_parsed,
                                    unparsed_marker=unparsed_marker,
                                ),
                                "jobs": self._merge_jobs([], extracted),
                                "totals": [int(value) for value in totals[:5]],
                                "has_more": [bool(value) for value in has_more[:5]],
                            }
                        )
                    except Exception:
                        return
                page.on("response", collect_response)
                navigated = self._goto_with_retry(page, PWTimeout)
                self.resolved_source_url = str(getattr(page, "url", "") or self.resolved_source_url)
                page.wait_for_timeout(self._remaining_ms(2500))
                self._wait_for_async_list(page)
                self.resolved_source_url = str(getattr(page, "url", "") or self.resolved_source_url)

                access_code = self._access_block_code(page)
                if access_code:
                    self._set_access_block(access_code)
                    close_browser()
                    return []

                if urlsplit(page.url).path.rstrip("/") in {"", "/"}:
                    entries = self._recruitment_entry_urls(page.content(), page.url)
                    if entries:
                        try:
                            page.goto(
                                entries[0],
                                wait_until="domcontentloaded",
                                timeout=self._remaining_ms(30000),
                            )
                            self.resolved_source_url = str(getattr(page, "url", "") or entries[0])
                            page.wait_for_timeout(self._remaining_ms(1800))
                            self._wait_for_async_list(page)
                        except Exception:
                            pass
                pre_scope_observation_count = len(network_observations)
                self._run_observed_entry_interaction(page)
                if self.recruitment_scope_changed:
                    # The URL may stay unchanged when a SPA switches from social
                    # to campus recruitment. Discard only pre-click API rows.
                    del network_observations[:pre_scope_observation_count]
                lazy_result = self._settle_lazy_list(page) or {}
                self._wait_for_async_list(page)
                self.resolved_source_url = str(getattr(page, "url", "") or self.resolved_source_url)
                access_code = self._access_block_code(page)
                if access_code:
                    self._set_access_block(access_code)
                    close_browser()
                    return []
                self.advertised_total = self._advertised_job_total(page.content())
                category_result = self._traverse_category_lists(page, lazy_result)
                lazy_results = list(category_result.get("lazy_results") or [lazy_result])
                self.category_scope_complete = category_result.get("categories_completed")
                self._active_category_scope = ""
                lazy_html = list(category_result.get("html_snapshots") or [])
                lazy_blocked_terms = {
                    "lazy_budget_exhausted", "lazy_round_limit", "load_more_stalled",
                    "lazy_observation_failed",
                }
                lazy_blocked = any(
                    item.get("termination") in lazy_blocked_terms
                    for item in lazy_results
                    if isinstance(item, dict)
                )
                current_html = page.content()
                selector_sig = self._pick_selector("\n".join([*lazy_html, current_html]))
                if not selector_sig:
                    selector_fallback = True
                    selector_sig = ""
                    discovered_jobs: list[dict] = []
                    discovered_seen: set[tuple[str, ...]] = set()
                    for html in [*lazy_html, current_html]:
                        self._parse(
                            html,
                            selector_sig,
                            discovered_jobs,
                            discovered_seen,
                            source_url=str(getattr(page, "url", "") or self.resolved_source_url),
                        )
                        discovered_jobs = self._merge_jobs(
                            discovered_jobs,
                            self._parse_line_jobs(html),
                        )
                    network_jobs, network_total, network_more, _, network_conflict = self._select_network_observation(
                        network_observations, discovered_jobs
                    )
                    discovered_jobs = self._merge_jobs(discovered_jobs, network_jobs)
                    if discovered_jobs:
                        if self.advertised_total is None and network_total is not None:
                            self.advertised_total = network_total
                    # Keep the selector-less run on the same pagination state
                    # machine.  A line/structured fallback is partial evidence,
                    # not a reason to abandon later pages or load-more states.
                else:
                    discovered_jobs = []
                    network_more = False
                    network_conflict = False

                seen_content_keys: set[str] = set()
                for _ in range(self.MAX_PAGES):
                    if time.monotonic() >= self._crawl_deadline:
                        self.has_more = True
                        self.pagination_termination_reason = "hard_timeout"
                        break
                    self.pages_seen += 1
                    if self.pages_seen == 1:
                        page_lazy_result = lazy_result
                        page_lazy_html = list(lazy_html)
                    else:
                        self._wait_for_async_list(page)
                        page_lazy_result = self._settle_lazy_list(page) or {}
                        lazy_results.append(page_lazy_result)
                        lazy_blocked = lazy_blocked or (
                            page_lazy_result.get("termination") in lazy_blocked_terms
                        )
                        page_lazy_html = list(page_lazy_result.get("html_snapshots") or [])
                    html = page.content()
                    source_url = str(getattr(page, "url", "") or self.resolved_source_url)
                    page_snapshots = [*page_lazy_html, html]
                    page_snapshot_keys: set[str] = set()
                    page_new_snapshot = False
                    before_count = len(jobs)
                    network_detail_links = self._network_detail_links(
                        network_observations, source_url
                    )
                    page_detail_pending: set[str] = set()
                    click_links = (
                        self._observe_click_detail_links(
                            page,
                            html,
                            selector_sig,
                            source_url,
                            network_detail_links=network_detail_links,
                            detail_pending=page_detail_pending,
                        )
                        if selector_sig
                        else {}
                    )
                    for page_html in page_snapshots:
                        snapshot = self._page_snapshot(page_html, selector_sig, source_url)
                        snapshot_key = snapshot[1]
                        if snapshot_key and snapshot_key in page_snapshot_keys:
                            continue
                        if snapshot_key:
                            page_snapshot_keys.add(snapshot_key)
                        if snapshot_key and snapshot_key in seen_content_keys:
                            continue
                        if snapshot_key:
                            seen_content_keys.add(snapshot_key)
                            page_new_snapshot = True
                        self._parse(
                            page_html,
                            selector_sig,
                            jobs,
                            seen,
                            source_url=source_url,
                            detail_links=click_links,
                            api_detail_links=network_detail_links,
                            detail_pending=page_detail_pending,
                        )
                    if self.pages_seen > 1 and not page_new_snapshot:
                        self.has_more = True
                        self.pagination_termination_reason = "repeated_page"
                        self._record_pagination_diagnostic(
                            page=self.pages_seen,
                            count=len(jobs),
                            total=self.advertised_total,
                            reason="repeated_page",
                            changed=False,
                            source_url=source_url,
                        )
                        break
                    new = max(0, len(jobs) - before_count)
                    self.resolved_source_url = source_url
                    self._record_pagination_diagnostic(
                        page=self.pages_seen,
                        count=max(0, len(jobs) - before_count),
                        total=self.advertised_total,
                        reason="page_observed",
                        changed=True,
                        source_url=source_url,
                    )
                    # Locate a fresh next control after every render; numbered buttons/ellipsis are never enumerated.
                    nxt = self._find_next_control(page)
                    try:
                        if nxt is None or nxt.count() == 0:
                            self.pagination_termination_reason = "no_next_control"
                            break
                        if self._next_is_disabled(nxt):
                            self.pagination_termination_reason = "next_disabled"
                            break
                        nxt.click(timeout=self._remaining_ms(4000))
                        if not self._wait_for_page_change(
                            page,
                            self._page_snapshot(html, selector_sig, source_url),
                            selector_sig,
                        ):
                            self.has_more = True
                            self.pagination_termination_reason = "page_unchanged"
                            self._record_pagination_diagnostic(
                                page=self.pages_seen,
                                count=new,
                                total=self.advertised_total,
                                reason="page_unchanged",
                                changed=False,
                                source_url=source_url,
                            )
                            break
                        self.resolved_source_url = str(getattr(page, "url", "") or source_url)
                    except Exception:
                        self.has_more = True
                        self.pagination_termination_reason = "next_navigation_failed"
                        break
                else:
                    self.has_more = True
                    self.pagination_termination_reason = "max_pages_reached"

                if not jobs:
                    jobs.extend(self._parse_line_jobs(page.content()))
                network_jobs, network_total, network_more, _, network_conflict = self._select_network_observation(
                    network_observations, jobs
                )
                jobs = self._merge_jobs(jobs, network_jobs)
                if self.advertised_total is None and network_total is not None:
                    self.advertised_total = network_total
                elif (
                    self.advertised_total is not None
                    and network_total is not None
                    and self.advertised_total != network_total
                ):
                    network_conflict = True

                final_html = page.content()
                terminal_reason = self.pagination_termination_reason
                static_scope_evidence = self._static_list_completion_evidence(
                    final_html,
                    lazy_result=(lazy_results[-1] if lazy_results else None),
                )
                lazy_complete_evidence = bool(lazy_results) and all(
                    bool(item.get("complete_evidence"))
                    for item in lazy_results
                    if isinstance(item, dict)
                )
                lazy_hard_stop = any(
                    isinstance(item, dict)
                    and item.get("termination") in {
                        "lazy_budget_exhausted", "lazy_round_limit", "load_more_stalled",
                        "lazy_observation_failed",
                    }
                    for item in lazy_results
                )
                lazy_view_incomplete = any(
                    isinstance(item, dict)
                    and item.get("termination") == "lazy_scroll_stalled"
                    for item in lazy_results
                ) and not static_scope_evidence and not (
                    self.advertised_total is not None and len(jobs) == self.advertised_total
                )
                blocked_from_complete = terminal_reason in {
                    "hard_timeout", "page_unchanged", "repeated_page",
                    "next_navigation_failed", "max_pages_reached",
                } or lazy_hard_stop or lazy_view_incomplete
                category_incomplete = self.category_scope_complete is False
                category_complete_evidence = self.category_scope_complete is True
                total_matches = (
                    self.advertised_total is not None and len(jobs) == self.advertised_total
                )
                has_real_jobs = bool(jobs)
                if terminal_reason == "next_disabled":
                    self.pagination_complete = has_real_jobs and (
                        total_matches if self.advertised_total is not None else True
                    )
                elif terminal_reason == "no_next_control":
                    self.pagination_complete = (
                        has_real_jobs and total_matches
                        if self.advertised_total is not None
                        else True
                        if has_real_jobs
                        and (lazy_complete_evidence or static_scope_evidence or category_complete_evidence)
                        else None
                    )
                else:
                    self.pagination_complete = False
                if (
                    network_more
                    or network_conflict
                    or category_incomplete
                    or (blocked_from_complete and self.pagination_complete is not None)
                ):
                    self.pagination_complete = False
                if self.pagination_complete:
                    self.has_more = False
                elif self.pagination_complete is None:
                    self.has_more = False
                    if terminal_reason == "no_next_control":
                        self.pagination_termination_reason = (
                            "line_fallback_completeness_unknown"
                            if selector_fallback
                            else "completeness_unknown"
                        )
                else:
                    self.has_more = True
                    if terminal_reason in {"next_disabled", "no_next_control"}:
                        if network_conflict:
                            self.pagination_termination_reason = "pagination_evidence_conflict"
                        elif category_incomplete:
                            self.pagination_termination_reason = "category_scope_incomplete"
                        elif terminal_reason == "hard_timeout":
                            self.pagination_termination_reason = "hard_timeout"
                        elif self.advertised_total is not None and not total_matches:
                            self.pagination_termination_reason = "advertised_total_mismatch"
                        elif selector_fallback and (lazy_hard_stop or lazy_view_incomplete):
                            self.pagination_termination_reason = "line_fallback_lazy_incomplete"
                        elif terminal_reason == "no_next_control":
                            self.pagination_termination_reason = (
                                "line_fallback_completeness_unknown"
                                if selector_fallback
                                else "completeness_unproven"
                            )
                if self.pagination_diagnostics:
                    self.pagination_diagnostics[-1]["reason"] = self.pagination_termination_reason[:80]

                close_browser()
        except Exception as e:  # noqa: BLE001
            self.fetch_failed = True
            self.pagination_complete = False
            self.pagination_termination_reason = (
                "hard_timeout"
                if time.monotonic() >= self._crawl_deadline
                else "render_exception"
            )
            if page is not None:
                self.resolved_source_url = str(getattr(page, "url", "") or self.resolved_source_url)
            logger.error("[%s] 渲染爬取异常: %s", self.company_name, e)
        finally:
            close_browser()

        logger.info("[%s] 通用渲染 抓到 %d 个岗位（选择器=%s）",
                    self.company_name, len(jobs), selector_sig)
        return jobs

    @staticmethod
    def _dict_ci(value: dict) -> dict[str, object]:
        return {str(key).casefold(): item for key, item in value.items()}

    @classmethod
    def _first_api_value(cls, values: dict[str, object], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = values.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _extract_api_payload(
        self,
        payload: object,
        response_url: str,
    ) -> tuple[list[dict], list[int], list[bool]]:
        """Extract job rows and list-completeness facts from common SPA JSON shapes."""

        jobs: list[dict] = []
        total_candidates: list[tuple[int, int]] = []
        has_more_candidates: list[tuple[int, bool]] = []

        def walk(value: object, depth: int = 0) -> int:
            if isinstance(value, list):
                return sum(walk(item, depth + 1) for item in value)
            if not isinstance(value, dict):
                return 0
            lowered = self._dict_ci(value)
            descendant_count = sum(walk(item, depth + 1) for item in value.values())
            title_raw = self._first_api_value(lowered, self._API_TITLE_KEYS)
            title = self._clean_title(title_raw)
            title_key = next(
                (key for key in self._API_TITLE_KEYS if str(lowered.get(key) or "").strip()),
                "",
            )
            strong_record_signal = set(lowered).intersection(
                self._API_SIGNAL_KEYS
                - {"id", "category", "jobtype", "job_type", "name", "title"}
            )
            explicit_record_id = set(lowered).intersection(
                {
                    "jobid", "job_id", "jobadid", "job_ad_id", "positionid", "position_id",
                    "postid", "post_id", "pid", "recruitmentid", "recruitment_id",
                }
            )
            own_count = 0
            if title and (strong_record_signal or explicit_record_id or title_key not in {"title", "name"}):
                city = self._first_api_value(lowered, self._API_CITY_KEYS)
                detail_parts = [
                    str(lowered[key]).strip()
                    for key in self._API_DETAIL_KEYS
                    if lowered.get(key) is not None and str(lowered[key]).strip()
                ]
                raw_url = self._first_api_value(lowered, self._API_URL_KEYS)
                candidate_url = (
                    urljoin(response_url, raw_url)
                    if raw_url and not _PLACEHOLDER_SOURCE_ID_RE.fullmatch(raw_url)
                    else ""
                )
                detail_observed = bool(
                    candidate_url and self._valid_observed_detail_url(candidate_url, self._list_url())
                )
                detail_url = candidate_url if detail_observed else self._list_url()
                job = self._make_job(
                    title=title,
                    city=city[:80],
                    jd_url=detail_url,
                    jd_raw="\n".join(dict.fromkeys(detail_parts))[:12000],
                    link_kind="detail" if detail_observed else "list",
                )
                native_id = self._first_api_value(
                    lowered,
                    ("jobid", "job_id", "positionid", "position_id", "postid", "post_id", "pid", "id"),
                )
                native_id = self._clean_source_id(native_id)
                if native_id:
                    job["native_job_id"] = native_id
                if detail_observed:
                    job["detail_href"] = raw_url[:2_048]
                    job["detail_link_api_observed"] = True
                jobs.append(job)
                own_count = 1
            subtree_count = descendant_count + own_count
            if subtree_count:
                for key in ("total", "totalcount", "total_count", "recordcount", "records"):
                    try:
                        total = int(lowered.get(key))
                    except (TypeError, ValueError):
                        continue
                    if total >= subtree_count:
                        total_candidates.append((depth, total))
                        break
                for key in ("hasmore", "has_more", "more"):
                    if key not in lowered:
                        continue
                    raw = lowered[key]
                    has_more_candidates.append(
                        (
                            depth,
                            raw if isinstance(raw, bool) else str(raw).casefold() in {"1", "true", "yes"},
                        )
                    )
                    break
            return subtree_count

        walk(payload)
        if total_candidates:
            outer_depth = min(depth for depth, _ in total_candidates)
            totals = list(dict.fromkeys(
                total for depth, total in total_candidates if depth == outer_depth
            ))
        else:
            totals = []
        if has_more_candidates:
            outer_depth = min(depth for depth, _ in has_more_candidates)
            has_more_values = [
                value for depth, value in has_more_candidates if depth == outer_depth
            ]
        else:
            has_more_values = []
        return self._merge_jobs([], jobs), totals, has_more_values

    @staticmethod
    def _job_base_key(job: dict) -> tuple[str, str]:
        return (
            str(job.get("title") or "").strip().casefold(),
            str(job.get("city") or "").strip().casefold(),
        )

    def _select_network_observation(
        self,
        observations: list[dict],
        dom_jobs: list[dict],
    ) -> tuple[list[dict], int | None, bool, str, bool]:
        """Select one listing scope; never combine unrelated response totals by maximum."""
        groups: dict[tuple[str, str], dict] = {}
        dom_titles = {
            str(job.get("title") or "").strip().casefold()
            for job in dom_jobs
            if str(job.get("title") or "").strip()
        }
        for observation in observations:
            source_scope = str(observation.get("source_scope") or "")
            api_scope = str(observation.get("api_scope") or "")
            if not source_scope or not api_scope:
                continue
            key = (source_scope, api_scope)
            group = groups.setdefault(
                key,
                {"jobs": [], "totals": [], "has_more": [], "events": [], "overlap": 0},
            )
            observation_totals = [
                value for value in (observation.get("totals") or [])
                if isinstance(value, int) and value >= 0
            ]
            observation_has_more = [
                bool(value) for value in (observation.get("has_more") or [])
            ]
            group["jobs"] = self._merge_jobs(group["jobs"], observation.get("jobs") or [])
            group["totals"].extend(observation_totals)
            group["has_more"].extend(observation_has_more)
            group["events"].append(
                {"totals": observation_totals, "has_more": observation_has_more}
            )
            group["overlap"] = sum(
                1 for job in group["jobs"] if self._job_base_key(job)[0] in dom_titles
            )
        if not groups:
            return [], None, False, "", False

        if self.category_scope_complete is True:
            category_groups: dict[str, list[tuple[tuple[str, str], dict]]] = defaultdict(list)
            for key, group in groups.items():
                source_scope, marker, _category = key[0].partition("#category:")
                if marker:
                    category_groups[source_scope].append((key, group))
            if category_groups:
                family_scores = {
                    source_scope: (
                        sum(int(group["overlap"]) for _, group in members),
                        sum(len(group["jobs"]) for _, group in members),
                    )
                    for source_scope, members in category_groups.items()
                }
                selected_source = max(family_scores, key=lambda value: family_scores[value])
                selected_groups = category_groups[selected_source]
                selected_jobs: list[dict] = []
                category_totals: list[int] = []
                latest_has_more_values: list[bool] = []
                conflict = False
                for _key, group in selected_groups:
                    selected_jobs = self._merge_jobs(selected_jobs, group["jobs"])
                    distinct_totals = list(dict.fromkeys(group["totals"]))
                    if len(distinct_totals) > 1:
                        conflict = True
                    if distinct_totals:
                        category_totals.append(int(distinct_totals[-1]))
                    for event in reversed(group["events"]):
                        if event["has_more"]:
                            latest_has_more_values.append(bool(event["has_more"][-1]))
                            break
                total: int | None = None
                if category_totals:
                    # Some APIs repeat the company-wide total for every tab;
                    # distinct category totals are additive only when they
                    # actually differ.
                    total = (
                        category_totals[0]
                        if len(set(category_totals)) == 1
                        else sum(category_totals)
                    )
                has_more = any(latest_has_more_values)
                if total is not None and len(selected_jobs) < total:
                    has_more = True
                elif total is not None and has_more:
                    conflict = True
                scope_hash = hashlib.sha256(
                    "\x1f".join(sorted(key[0] + "\x1e" + key[1] for key, _ in selected_groups)).encode("utf-8")
                ).hexdigest()[:12]
                return selected_jobs, total, has_more, scope_hash, conflict

        source_scores: dict[str, tuple[int, int]] = {}
        for (source_scope, _), group in groups.items():
            score = source_scores.get(source_scope, (0, 0))
            source_scores[source_scope] = (
                score[0] + int(group["overlap"]),
                score[1] + len(group["jobs"]),
            )
        selected_source = max(source_scores, key=lambda value: source_scores[value])
        candidates = {
            key: group for key, group in groups.items() if key[0] == selected_source
        }
        selected_key = max(
            candidates,
            key=lambda key: (
                int(candidates[key]["overlap"]),
                len(candidates[key]["jobs"]),
                len(candidates[key]["totals"]),
            ),
        )
        selected = candidates[selected_key]
        total: int | None = None
        conflict = False
        distinct_totals = list(dict.fromkeys(selected["totals"]))
        if distinct_totals:
            total = int(distinct_totals[-1])
            conflict = len(distinct_totals) > 1

        latest_has_more: bool | None = None
        for event in reversed(selected["events"]):
            if event["has_more"]:
                latest_has_more = bool(event["has_more"][-1])
                break
        has_more = latest_has_more if latest_has_more is not None else False
        if total is not None and len(selected["jobs"]) < total:
            has_more = True
        elif total is not None:
            if latest_has_more is True:
                conflict = True
            has_more = False
        scope_hash = hashlib.sha256(
            f"{selected_key[0]}\x1f{selected_key[1]}".encode("utf-8")
        ).hexdigest()[:12]
        return selected["jobs"], total, has_more, scope_hash, conflict

    @classmethod
    def _merge_jobs(cls, primary: list[dict], extra: list[dict]) -> list[dict]:
        merged: list[dict] = []
        base_indexes: dict[tuple[str, str], list[int]] = defaultdict(list)
        for job in [*primary, *extra]:
            base_key = cls._job_base_key(job)
            if not base_key[0]:
                continue
            duplicate_index = None
            for index in base_indexes.get(base_key, []):
                existing = merged[index]
                existing_identity = cls._job_identity_key(existing)
                job_identity = cls._job_identity_key(job)
                if (
                    existing_identity == job_identity
                    or existing_identity[0] == "title_city"
                    or job_identity[0] == "title_city"
                ):
                    duplicate_index = index
                    break
            if duplicate_index is not None:
                if (
                    cls._job_identity_key(job)[0] != "title_city"
                    and cls._job_identity_key(merged[duplicate_index])[0] == "title_city"
                ):
                    replacement = dict(job)
                    for field, value in merged[duplicate_index].items():
                        if field not in replacement or replacement[field] in (None, "", [], {}):
                            replacement[field] = value
                    merged[duplicate_index] = replacement
                continue
            base_indexes[base_key].append(len(merged))
            merged.append(job)
        return merged

    @staticmethod
    def _advertised_job_total(html: str) -> int | None:
        """Read conservative list-total labels, never arbitrary numbers from the page."""

        soup = BeautifulSoup(html, "html.parser")
        patterns = (
            r"(?:在招职位|在招岗位)\s*[（(:：]?\s*(\d{1,5})",
            r"(?<!\d)(\d{1,5})\s*(?:个|条)?\s*(?:在招职位|在招岗位)",
            r"共\s*(\d{1,5})\s*(?:条|个)?\s*(?:岗位|职位|工作)",
            r"(?:岗位|职位|工作)\s*(?:总数|数量)?\s*[（(：:]\s*(\d{1,5})\s*[）)]?",
            r"(?:开启新的工作|open\s+positions?|jobs?)\s*[（(]\s*(\d{1,5})\s*[）)]",
        )
        totals = set()
        for node in soup.find_all(["div", "p", "span", "h1", "h2", "h3", "h4", "label", "li"]):
            text = node.get_text(" ", strip=True)
            if len(text) > 100 or re.search(r"投递|申请|每人|最多|广告|届|年度", text):
                continue
            if node.find_parent(["header", "footer", "nav"]) or node.find_parent(class_=_CARD_CLASS):
                continue
            if not all(
                GenericRenderCrawler._html_node_visible(parent)
                for parent in (node, *node.parents)
                if getattr(parent, "attrs", None) is not None
            ):
                continue
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    totals.add(int(match.group(1)))
        return totals.pop() if len(totals) == 1 else None

    @staticmethod
    def _recruitment_entry_urls(html: str, base_url: str) -> list[str]:
        """Rank recruitment navigation links found on a company home page."""

        soup = BeautifulSoup(html, "html.parser")
        candidates: list[tuple[int, str]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            href = (anchor.get("href") or "").strip()
            if not href or href == "#" or href.casefold().startswith(("javascript:", "mailto:")):
                continue
            resolved = urljoin(base_url, href)
            parsed = urlsplit(resolved)
            if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() in _NON_JOB_HOSTS:
                continue
            signal = f"{text} {parsed.path} {parsed.fragment}".casefold()
            score = 0
            if re.search(r"校园招聘|校招|应届|campus", signal, re.I):
                score += 100
            if re.search(r"招聘|人才|职位|岗位|career|recruit|jobs?", signal, re.I):
                score += 50
            if re.search(r"加入我们|join\s*us", signal, re.I):
                score += 25
            if re.search(r"社会招聘|社招|experienced", signal, re.I):
                score -= 40
            if score <= 0 or resolved in seen:
                continue
            seen.add(resolved)
            candidates.append((score, resolved))
        candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return [url for _, url in candidates[:5]]

    @staticmethod
    def _category_control_key(node, owner=None) -> str:
        attrs = getattr(node, "attrs", {}) or {}
        for key in (
            "data-category-id", "data-category", "data-type", "data-job-type",
            "data-tab", "data-value", "href",
        ):
            value = str(attrs.get(key) or "").strip()
            if value and value != "#":
                return value
        owner_attrs = getattr(owner, "attrs", {}) or {}
        owner_key = str(owner_attrs.get("id") or " ".join(owner_attrs.get("class") or [])).strip()
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        return f"{owner_key}\x1f{text}"

    @classmethod
    def _category_control_is_candidate(cls, node) -> bool:
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not 1 <= len(text) <= 60 or _NON_JOB_TITLE.fullmatch(text):
            return False
        if node.name not in {"a", "button", "li"} and node.get("role") != "tab":
            return False
        signal = " ".join(
            [
                str(node.get("role") or ""),
                str(node.get("id") or ""),
                " ".join(str(value) for value in (node.get("class") or [])),
                str(node.get("data-category") or ""),
                str(node.get("data-category-id") or ""),
                str(node.get("data-type") or ""),
            ]
        )
        if not re.search(r"tab|categor|job[-_ ]?type|position[-_ ]?type", signal, re.I):
            owner = node.parent
            found_owner = False
            for _ in range(2):
                if owner is None:
                    break
                owner_signal = " ".join(
                    [
                        str(owner.get("id") or ""),
                        " ".join(str(value) for value in (owner.get("class") or [])),
                        str(owner.get("role") or ""),
                    ]
                )
                if re.search(r"tab|categor|job[-_ ]?type|position[-_ ]?type", owner_signal, re.I):
                    found_owner = True
                    break
                owner = owner.parent
            if not found_owner:
                return False
        return not bool(
            re.search(r"首页|关于我们|联系我们|新闻|登录|注册|投递方式|招聘流程|社会招聘|社招", text, re.I)
        )

    def _category_controls(self, page) -> list[dict[str, object]]:
        """Find repeated visible category tabs without depending on a company name."""

        script = r"""/* recruitops-category-controls */
        () => {
          const visible = node => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && rect.width > 0 && rect.height > 0;
          };
          const textOf = node => (node.innerText || node.textContent || '')
            .replace(/\s+/g, ' ').trim();
          const selectors = [
            '[role="tab"]', '[role="tablist"] > *', '[data-category]',
            '[data-category-id]', '[data-job-type]', '[data-tab]',
            '[class*="tab"] li', '[class*="tabs"] li',
            '[class*="category"] li', '[class*="job-type"] li',
            '[class*="tab"] button', '[class*="tabs"] button',
            '[class*="category"] button', '[class*="job-type"] button'
          ];
          const nodes = [];
          const seen = new Set();
          for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (seen.has(node) || !visible(node)) continue;
              const text = textOf(node);
              if (!text || text.length > 60 || /首页|关于我们|联系我们|新闻|登录|注册|投递方式|招聘流程|社会招聘|社招/i.test(text)) continue;
              const owner = node.closest('[role="tablist"], [class*="tab"], [class*="category"], [class*="type"], [data-tab-group]');
              if (!owner || /pagination|pager/i.test(`${owner.id} ${owner.className || ''}`)) continue;
              if (node.getAttribute('role') !== 'tab' && node.parentElement !== owner && node.parentElement?.parentElement !== owner) continue;
              const attrs = node.dataset || {};
              const ownerKey = `${owner.id || ''}|${owner.className || ''}`;
              const key = attrs.categoryId || attrs.category || attrs.jobType || attrs.tab
                || node.getAttribute('href') || `${ownerKey}\u001f${text}`;
              const selected = node.getAttribute('aria-selected') === 'true'
                || /(^|\s)(active|selected|current|on|select-color)(\s|$)/i.test(node.className || '');
              seen.add(node);
              nodes.push({index: nodes.length, key: String(key), text, selected,
                disabled: !!node.disabled || node.getAttribute('aria-disabled') === 'true'});
            }
          }
          return nodes;
        }"""
        try:
            raw = page.evaluate(script)
        except Exception:
            raw = []
        controls: list[dict[str, object]] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    continue
                text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                key = str(item.get("key") or text).strip()
                if not text or not key or not 1 <= len(text) <= 60:
                    continue
                controls.append(
                    {
                        "index": int(item.get("index", index)),
                        "key": key,
                        "text": text,
                        "selected": bool(item.get("selected")),
                        "disabled": bool(item.get("disabled")),
                    }
                )
        if controls:
            return controls

        # Keep deterministic test doubles and server-rendered pages on the
        # same structural path when their page object does not implement JS.
        try:
            soup = BeautifulSoup(page.content(), "html.parser")
        except Exception:
            return []
        candidates = []
        for node in soup.find_all(["a", "button", "li", "span", "div"]):
            if not self._html_node_visible(node) or not self._category_control_is_candidate(node):
                continue
            if node.name in {"div", "span"} and len(node.find_all(["li", "button"], recursive=True)) >= 2:
                continue
            owner = node
            while owner and not re.search(
                r"tab|categor|job[-_ ]?type|position[-_ ]?type",
                " ".join(str(value) for value in (owner.get("class") or []))
                + " " + str(owner.get("id") or ""),
                re.I,
            ):
                owner = owner.parent
            key = self._category_control_key(node, owner)
            candidates.append(
                {
                    "index": len(candidates),
                    "key": key,
                    "text": re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip(),
                    "selected": bool(
                        str(node.get("aria-selected") or "").casefold() == "true"
                        or re.search(r"(?:^|\s)(?:active|selected|current|on|select-color)(?:\s|$)",
                                     " ".join(node.get("class") or []), re.I)
                    ),
                    "disabled": bool(node.has_attr("disabled")),
                }
            )
        unique: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        for item in candidates:
            if item["key"] in seen_keys:
                continue
            seen_keys.add(str(item["key"]))
            item["index"] = len(unique)
            unique.append(item)
        return unique

    def _click_category_control(self, page, control: dict[str, object]) -> bool:
        key = str(control.get("key") or "")
        text = str(control.get("text") or "")
        index = int(control.get("index") or 0)
        script = f"""/* recruitops-category-click */
        (expected) => {{
          const visible = node => {{
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && rect.width > 0 && rect.height > 0;
          }};
          const textOf = node => (node.innerText || node.textContent || '')
            .replace(/\\s+/g, ' ').trim();
          const selectors = ['[role="tab"]', '[role="tablist"] > *', '[data-category]',
            '[data-category-id]', '[data-job-type]', '[data-tab]', '[class*="tab"] li',
            '[class*="tabs"] li', '[class*="category"] li', '[class*="job-type"] li',
            '[class*="tab"] button', '[class*="tabs"] button',
            '[class*="category"] button', '[class*="job-type"] button'];
          const nodes = [];
          const seen = new Set();
          for (const selector of selectors) for (const node of document.querySelectorAll(selector)) {{
            if (seen.has(node) || !visible(node)) continue;
            const owner = node.closest('[role="tablist"], [class*="tab"], [class*="category"], [class*="type"], [data-tab-group]');
            if (!owner || /pagination|pager/i.test(`${{owner.id}} ${{owner.className || ''}}`)) continue;
            if (node.getAttribute('role') !== 'tab' && node.parentElement !== owner && node.parentElement?.parentElement !== owner) continue;
            const nodeText = textOf(node);
            if (!nodeText || /首页|关于我们|联系我们|新闻|登录|注册|投递方式|招聘流程|社会招聘|社招/i.test(nodeText)) continue;
            const attrs = node.dataset || {{}};
            const ownerKey = `${{owner.id || ''}}|${{owner.className || ''}}`;
            const nodeKey = String(attrs.categoryId || attrs.category || attrs.jobType || attrs.tab
              || node.getAttribute('href') || `${{ownerKey}}\\u001f${{nodeText}}`);
            if (nodeKey === expected.key) {{
              node.click();
              return true;
            }}
            seen.add(node);
            nodes.push({{node, text: nodeText, key: nodeKey}});
          }}
          const matches = nodes.filter(item => item.text === expected.text);
          if (matches.length === 1) {{ matches[0].node.click(); return true; }}
          return false;
        }}"""
        try:
            result = page.evaluate(script, {"index": index, "text": text, "key": key})
            if result:
                return True
        except Exception:
            pass
        # A narrow exact-text fallback is useful for older Playwright doubles;
        # it never clicks a broad substring or an arbitrary page element.
        if text:
            try:
                target = page.get_by_text(re.compile(rf"^\s*{re.escape(text)}\s*$")).first
                if target.count() and target.is_visible():
                    target.click(timeout=self._remaining_ms(2500))
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _category_scope_key(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:16]

    def _static_list_completion_evidence(
        self,
        html: str,
        *,
        lazy_result: dict | None = None,
    ) -> bool:
        """Accept a static page only when its list structure is inspectably terminal."""

        if not html or self._classify_access_block(html):
            return False
        if lazy_result:
            termination = str(lazy_result.get("termination") or "")
            if termination in {
                "lazy_budget_exhausted", "lazy_round_limit", "load_more_stalled",
                "lazy_observation_failed",
            }:
                return False
            if lazy_result.get("scroll_moved") or len(lazy_result.get("html_snapshots") or []) > 1:
                # A changing viewport is an observation window, not proof that
                # the source list has ended.
                return False
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.find_all(["button", "a", "input", "nav", "div", "ul", "ol"]):
            if not self._html_node_visible(node):
                continue
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True) or str(node.get("value") or "")).strip()
            signal = " ".join(
                [
                    str(node.get("id") or ""),
                    " ".join(str(value) for value in (node.get("class") or [])),
                    str(node.get("rel") or ""),
                    str(node.get("aria-label") or ""),
                    str(node.get("title") or ""),
                    text,
                ]
            )
            if re.search(
                r"加载更多|更多职位|显示更多|load\s+more|show\s+more|"
                r"pagination|pager|下一页|上一页|\bnext\b|\bprev(?:ious)?\b",
                signal,
                re.I,
            ):
                return False
        if soup.find(attrs={"aria-busy": re.compile(r"true", re.I)}) or soup.find(
            attrs={"data-loading": re.compile(r"true", re.I)}
        ):
            return False
        valid = [
            node
            for node in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"])
            if self._is_real_dom_candidate(node)[3]
        ]
        if not valid:
            return False
        bounded = [node for node in valid if self._is_bounded_job_card(node)]
        if not bounded:
            return False
        # A table or repeated card is not an end marker: lazy feeds use both.
        # Only accept an explicit terminal flag enclosing the observed jobs.
        for root in soup.select(
            '[data-list-complete="true"], [data-has-more="false"], '
            '[data-hasmore="false"]'
        ):
            if not all(
                self._html_node_visible(node)
                for node in (root, *root.parents)
                if getattr(node, "attrs", None) is not None
            ):
                continue
            if any(root in node.parents for node in bounded):
                return True
        return False

    def _traverse_category_lists(self, page, initial_lazy_result: dict | None) -> dict:
        """Visit each visible category and its own paging/load-more scope."""

        try:
            group = page.locator('[role="tablist"], [class*="tab"] > ul, [class*="category"] > ul').first
            if group.count():
                group.scroll_into_view_if_needed(timeout=self._remaining_ms(1500))
                page.wait_for_timeout(self._remaining_ms(200))
        except Exception:
            pass
        controls = self._category_controls(page)
        initial_html = page.content()
        base_url = str(getattr(page, "url", "") or self.resolved_source_url)
        result = {
            "html_snapshots": list((initial_lazy_result or {}).get("html_snapshots") or []),
            "lazy_results": [initial_lazy_result] if initial_lazy_result else [],
            "categories": [],
            "categories_completed": None,
            "category_controls_seen": len(controls),
            "scope_terminations": [],
        }
        if len(controls) < 2:
            if initial_html and initial_html not in result["html_snapshots"]:
                result["html_snapshots"].append(initial_html)
            return result

        result["categories_completed"] = True
        processed: set[str] = set()

        def add_html(html: str) -> None:
            if html and html not in result["html_snapshots"]:
                result["html_snapshots"].append(html)

        for control_index, control in enumerate(controls):
            key = str(control.get("key") or control.get("text") or control_index)
            if key in processed or bool(control.get("disabled")):
                result["categories_completed"] = False
                continue
            processed.add(key)
            text = str(control.get("text") or key)
            category_scope = self._category_scope_key(key)
            self._active_category_scope = category_scope
            before_html = page.content()
            before = self._page_snapshot(before_html, "", str(getattr(page, "url", "") or base_url))
            selected = bool(control.get("selected"))
            clicked = False
            changed = selected
            if not selected:
                clicked = self._click_category_control(page, control)
                if clicked:
                    self._wait_for_async_list(page)
                    after_html = page.content()
                    after = self._page_snapshot(
                        after_html,
                        "",
                        str(getattr(page, "url", "") or base_url),
                    )
                    active_keys = {
                        str(item.get("key") or "")
                        for item in self._category_controls(page)
                        if item.get("selected")
                    }
                    changed = self._snapshot_changed(before, after) or key in active_keys
                if not changed:
                    result["categories_completed"] = False
            lazy = (
                (initial_lazy_result or {})
                if selected and control_index == 0
                else self._settle_lazy_list(page) or {}
            )
            result["lazy_results"].append(lazy) if lazy is not initial_lazy_result else None
            for html in [*(lazy.get("html_snapshots") or []), page.content()]:
                add_html(html)

            scope_termination = str(lazy.get("termination") or "no_next_control")
            scope_complete = bool(lazy.get("complete_evidence"))
            page_count = 1
            while changed and page_count < self.MAX_PAGES:
                nxt = self._find_next_control(page)
                if nxt is None or nxt.count() == 0:
                    scope_termination = "no_next_control"
                    scope_complete = scope_complete or self._static_list_completion_evidence(
                        page.content(), lazy_result=lazy
                    )
                    break
                if self._next_is_disabled(nxt):
                    scope_termination = "next_disabled"
                    scope_complete = not (
                        str(lazy.get("termination") or "") in {
                            "lazy_budget_exhausted", "lazy_round_limit", "load_more_stalled",
                            "lazy_observation_failed",
                        }
                    )
                    break
                before_page = self._page_snapshot(
                    page.content(), "", str(getattr(page, "url", "") or base_url)
                )
                try:
                    nxt.click(timeout=self._remaining_ms(4000))
                except Exception:
                    scope_termination = "next_navigation_failed"
                    result["categories_completed"] = False
                    break
                if not self._wait_for_page_change(page, before_page, ""):
                    scope_termination = "page_unchanged"
                    result["categories_completed"] = False
                    break
                page_count += 1
                lazy = self._settle_lazy_list(page) or {}
                result["lazy_results"].append(lazy)
                for html in [*(lazy.get("html_snapshots") or []), page.content()]:
                    add_html(html)
                if lazy.get("termination") in {
                    "lazy_budget_exhausted", "lazy_round_limit", "load_more_stalled",
                    "lazy_observation_failed",
                }:
                    result["categories_completed"] = False
                scope_complete = scope_complete or bool(lazy.get("complete_evidence"))
            else:
                if page_count >= self.MAX_PAGES:
                    scope_termination = "max_pages_reached"
                    scope_complete = False

            if scope_termination == "no_next_control" and not scope_complete:
                scope_complete = self._static_list_completion_evidence(page.content(), lazy_result=lazy)
            if not scope_complete:
                result["categories_completed"] = False
            result["categories"].append(
                {
                    "key": key,
                    "text": text,
                    "scope": category_scope,
                    "clicked": clicked,
                    "changed": changed,
                    "pages_seen": page_count,
                    "termination": scope_termination,
                    "complete_evidence": bool(scope_complete),
                }
            )
            result["scope_terminations"].append(scope_termination)
            if text not in self.categories_seen:
                self.categories_seen.append(text)

        if len(processed) != len(controls):
            result["categories_completed"] = False
        return result

    def _settle_lazy_list(self, page) -> dict:
        """Traverse bounded lazy-list rounds and retain every observed DOM window.

        A stable scroll position is only an observation. Completeness requires a
        stronger signal such as a disabled load-more control or API evidence; the
        caller must combine this result with the pagination state machine.
        """

        source_url = str(getattr(page, "url", "") or self.resolved_source_url)
        snapshots: list[str] = []
        snapshot_signatures: set[str] = set()
        observed_keys: set[tuple[str, ...]] = set()
        stable_rounds = 0
        rounds = 0
        load_more_seen = False
        load_more_clicked = False
        scroll_moved = False
        termination = "lazy_round_limit"

        def capture() -> tuple[str, str, set[tuple[str, ...]]]:
            html = page.content()
            current_url = str(getattr(page, "url", "") or source_url)
            signature = self._lazy_list_signature(html, current_url)
            keys = self._lazy_dom_job_keys(html, current_url)
            if signature not in snapshot_signatures and len(snapshots) < self.MAX_LAZY_SNAPSHOTS:
                snapshots.append(html)
                snapshot_signatures.add(signature)
            observed_keys.update(keys)
            return html, signature, keys

        try:
            _, signature, _ = capture()
            for _ in range(self.MAX_LAZY_ROUNDS):
                rounds += 1
                round_start_signature = signature
                round_start_keys = set(observed_keys)
                if self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline:
                    termination = "lazy_budget_exhausted"
                    break

                load_state = self._load_more_state(page)
                load_more_seen = load_more_seen or load_state["found"]
                if load_state["disabled"]:
                    termination = "load_more_disabled"
                    break

                changed = False
                if load_state["clicked"]:
                    load_more_clicked = True
                    click_signature = signature
                    changed, signature = self._wait_for_lazy_signature(page, click_signature, source_url)
                    _, signature, _ = capture()
                    changed = changed or signature != click_signature
                    if not changed:
                        termination = "load_more_stalled"
                        break

                moved = self._advance_lazy_scroll(page)
                scroll_moved = scroll_moved or moved
                if moved:
                    scroll_signature = signature
                    scroll_changed, signature = self._wait_for_lazy_signature(page, scroll_signature, source_url)
                    _, signature, _ = capture()
                    scroll_changed = scroll_changed or signature != scroll_signature
                    changed = changed or scroll_changed
                    if not scroll_changed:
                        termination = "lazy_scroll_stalled"
                        break

                _, next_signature, next_keys = capture()
                new_keys = next_keys - round_start_keys
                signature_changed = next_signature != round_start_signature
                signature = next_signature
                if not changed and not moved and not signature_changed:
                    stable_rounds += 1
                else:
                    stable_rounds = 0

                if not load_state["found"] and load_more_clicked and not moved and stable_rounds >= 1:
                    termination = "load_more_exhausted"
                    break
                if not new_keys and stable_rounds >= 2:
                    termination = "lazy_scroll_stable"
                    break
            else:
                termination = "lazy_round_limit"
        except Exception:
            termination = "lazy_observation_failed"

        try:
            page.evaluate(
                """/* recruitops-lazy-restore */
                () => {
                    const root = document.scrollingElement;
                    for (const node of [root, ...document.querySelectorAll('*')].slice(0, 32)) {
                        if (node && node.scrollTop) node.scrollTop = 0;
                    }
                    window.scrollTo(0, 0);
                }"""
            )
        except Exception:
            pass
        return {
            "html_snapshots": snapshots,
            "observed_job_keys": observed_keys,
            "rounds": rounds,
            "load_more_seen": load_more_seen,
            "load_more_clicked": load_more_clicked,
            "scroll_moved": scroll_moved,
            "termination": termination,
            "complete_evidence": termination in {"load_more_disabled", "load_more_exhausted"},
        }

    def _goto_with_retry(self, page, timeout_error) -> bool:
        """Navigate with one retry for transient timeout/empty-response errors."""
        for attempt in range(1, self.NAVIGATION_ATTEMPTS + 1):
            if self._crawl_deadline is not None and time.monotonic() >= self._crawl_deadline:
                return False
            try:
                page.goto(
                    self._list_url(),
                    wait_until="domcontentloaded",
                    timeout=self._remaining_ms(30000),
                )
                return True
            except timeout_error as exc:
                reason = f"超时: {exc}"
            except Exception as exc:  # Playwright network errors are not timeouts.
                reason = str(exc)
            if attempt < self.NAVIGATION_ATTEMPTS:
                logger.warning(
                    "[%s] 页面打开失败，准备重试 %d/%d: %s",
                    self.company_name, attempt, self.NAVIGATION_ATTEMPTS - 1, reason,
                )
                page.wait_for_timeout(self._remaining_ms(1500))
        logger.warning(
            "[%s] 页面打开连续失败，仍尝试解析当前页面: %s",
            self.company_name, reason,
        )
        return False

    def _parse_line_jobs(self, html: str) -> list[dict]:
        """Fallback for simple static pages that list jobs as lines like `职位：软件工程师`."""
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        jobs, seen = [], set()
        for match in re.finditer(r"(?:^|\n)\s*职位\s*[：:]\s*([^\n\r]{2,40})", text):
            title = self._clean_title(match.group(1))
            if not title or title in seen:
                continue
            seen.add(title)
            jobs.append(self._make_job(
                title=title, city="", jd_url=self._list_url(), link_kind="list",
            ))
        return jobs

    def _sig(self, el) -> str:
        cls = ".".join(el.get("class") or [])
        return f"{el.name}.{cls}" if cls else el.name

    def _direct_text(self, el) -> str:
        direct = "".join(t for t in el.find_all(string=True, recursive=False)).strip()
        return direct if direct else el.get_text(" ", strip=True)

    def _click_campus_entry(self, page) -> bool:
        """Some sites default to social jobs even from a campus-looking URL."""
        for kw in ("校园招聘", "校招岗位", "校招职位", "应届生招聘", "校园职位"):
            try:
                loc = page.get_by_text(re.compile(rf"^\s*{re.escape(kw)}\s*$")).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                selected = str(loc.get_attribute("aria-selected") or "").casefold() == "true"
                classes = " ".join(filter(None, (
                    loc.get_attribute("class"),
                    loc.locator("xpath=..").get_attribute("class"),
                )))
                if selected or re.search(
                    r"(?:^|\s)(?:active|selected|current|select-color)(?:\s|$)", classes, re.I,
                ):
                    return False
                before = hashlib.sha256(page.content().encode("utf-8", errors="ignore")).hexdigest()
                loc.click(timeout=2500)
                self._wait_for_async_list(page)
                after = hashlib.sha256(page.content().encode("utf-8", errors="ignore")).hexdigest()
                selected = str(loc.get_attribute("aria-selected") or "").casefold() == "true"
                classes = " ".join(filter(None, (
                    loc.get_attribute("class"),
                    loc.locator("xpath=..").get_attribute("class"),
                )))
                changed = before != after or selected or bool(re.search(
                    r"(?:^|\s)(?:active|selected|current|select-color)(?:\s|$)", classes, re.I,
                ))
                self.recruitment_scope_changed = changed
                return changed
            except Exception:
                continue
        return False

    def _pick_selector(self, html: str) -> str:
        """Choose a repeated selector only after each hit has bounded job evidence."""
        soup = BeautifulSoup(html, "html.parser")
        score = defaultdict(int)
        for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            if self._is_real_dom_candidate(el)[3]:
                score[self._sig(el)] += 1
        if not score:
            return ""
        best, n = max(score.items(), key=lambda kv: kv[1])
        return best if n >= self.MIN_HITS else ""

    def _selector_job_count(self, html: str, sig: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        return sum(
            1
            for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"])
            if self._sig(el) == sig and self._is_real_dom_candidate(el)[3]
        )

    def _selector_parsed_count(self, html: str, sig: str) -> int:
        jobs: list[dict] = []
        self._parse(html, sig, jobs, set())
        return len(jobs)

    def _dom_job_record(
        self,
        el,
        title: str,
        context: str,
        source_url: str,
        selector_sig: str,
        index: int,
        *,
        detail_links: dict[str, str],
        api_detail_links: dict[str, tuple[str, str]],
        detail_pending: set[str],
    ) -> dict:
        jd_url, link_kind, observed, detail_href = self._job_link_observation(el, source_url)
        api_observed = False
        click_observed = False
        if title in detail_links and self._valid_observed_detail_url(detail_links[title], source_url):
            jd_url, link_kind, observed = detail_links[title], "detail", True
            observed = False
            click_observed = True
            detail_href = detail_links[title]
        elif not observed and title in api_detail_links:
            api_url, _api_jd = api_detail_links[title]
            if self._valid_observed_detail_url(api_url, source_url):
                jd_url, link_kind = api_url, "detail"
                api_observed = True
                detail_href = api_url
        labels = self._extract_dom_labels(el)
        city = self._extract_job_city(title, context)
        record = self._make_job(
            title=title,
            city=city,
            jd_url=jd_url,
            link_kind=link_kind,
            jd_raw=self._inline_jd(context, title) or (
                api_detail_links.get(title, ("", ""))[1] if api_observed else ""
            ),
            published_at=str(labels.get("published_at") or ""),
        )
        record["dom_labels"] = labels
        record["is_urgent"] = bool(labels.get("urgent"))
        record["is_negotiable"] = bool(labels.get("negotiable"))
        record["card_locator"] = self._card_locator(el, selector_sig, index)
        native_id = self._native_job_id(el)
        if native_id:
            record["native_job_id"] = native_id
        elif observed or click_observed or api_observed:
            source_job_id = self._observed_job_id_from_url(jd_url)
            if source_job_id:
                record["source_job_id"] = source_job_id
        if observed:
            record["detail_link_source_url"] = source_url
            record["detail_link_observed"] = True
        elif click_observed:
            record["detail_link_click_observed"] = True
            record["detail_click_source_url"] = source_url
        elif api_observed:
            record["detail_link_api_observed"] = True
        elif title in detail_pending:
            record["detail_pending"] = True
        if detail_href and (observed or click_observed or api_observed):
            record["detail_href"] = str(detail_href)[:2_048]
        return record

    def _parse_structured_list(
        self,
        html: str,
        jobs: list,
        seen: set,
        *,
        source_url: str,
        detail_links: dict[str, str],
        api_detail_links: dict[str, tuple[str, str]],
        detail_pending: set[str],
    ) -> int:
        """Parse bounded structured cards when no repeated title selector wins."""

        soup = BeautifulSoup(html or "", "html.parser")
        new = 0
        candidate_index = 0
        for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            title, context, _, valid = self._is_real_dom_candidate(el, source_url)
            if not valid:
                continue
            record = self._dom_job_record(
                el,
                title,
                context,
                source_url,
                self._sig(el),
                candidate_index,
                detail_links=detail_links,
                api_detail_links=api_detail_links,
                detail_pending=detail_pending,
            )
            candidate_index += 1
            key = self._job_identity_key(record)
            if key in seen:
                continue
            seen.add(key)
            jobs.append(record)
            new += 1
        return new

    def _parse(
        self,
        html: str,
        sig: str,
        jobs: list,
        seen: set,
        *,
        source_url: str | None = None,
        detail_links: dict[str, str] | None = None,
        api_detail_links: dict[str, tuple[str, str]] | None = None,
        detail_pending: set[str] | None = None,
    ) -> int:
        if not sig:
            parsed = self._parse_structured_list(
                html,
                jobs,
                seen,
                source_url=str(source_url or self._list_url()),
                detail_links=detail_links or {},
                api_detail_links=api_detail_links or {},
                detail_pending=detail_pending or set(),
            )
            # Some legacy pages expose only ``职位：...`` lines.  Keep those
            # rows on this same selector-less page path so later pages can
            # still be advanced and merged by their stable identity.
            for line_job in self._parse_line_jobs(html):
                key = self._job_identity_key(line_job)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(line_job)
                parsed += 1
            return parsed
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        source_url = str(source_url or self._list_url())
        detail_links = detail_links or {}
        api_detail_links = api_detail_links or {}
        detail_pending = detail_pending or set()
        selector_index = 0
        for el in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            if self._sig(el) != sig:
                continue
            current_index = selector_index
            selector_index += 1
            title, ctext, _, valid = self._is_real_dom_candidate(el, source_url)
            if not valid:
                continue
            record = self._dom_job_record(
                el,
                title,
                ctext,
                source_url,
                sig,
                current_index,
                detail_links=detail_links,
                api_detail_links=api_detail_links,
                detail_pending=detail_pending,
            )
            key = self._job_identity_key(record)
            if key in seen:
                continue
            seen.add(key)
            jobs.append(record)
            new += 1
        return new
