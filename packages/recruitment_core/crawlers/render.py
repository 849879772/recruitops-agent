import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any, Literal, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import configured_crawl_timeout_seconds, launch_browser

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 标准 stealth 注入：隐藏 navigator.webdriver 标志
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# 阻挡的资源类型（图片/字体能提速但保留 stylesheet：
# Playwright 的 wait_for_selector 需要 CSS 计算元素可见性，否则会误判超时）
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_RECRUITMENT_RESPONSE_RE = re.compile(r"job|position|recruit|career|campus|post|vacan", re.I)
_SENSITIVE_KEY_RE = re.compile(r"token|cookie|authorization|password|secret|session", re.I)
_DETAIL_INTERACTION_MODES = {"inline", "dialog", "tabs"}
_DANGEROUS_CONTROL_RE = re.compile(
    r"申请|投递|立即投递|登录|注册|apply|sign\s*in|log\s*in|submit",
    re.I,
)
_DETAIL_LOGIN_ROUTE_RE = re.compile(
    r"(?:^|[/#._-])(?:login|signin|sign-in|auth)(?:[/#?._&=-]|$)",
    re.I,
)
_DETAIL_NOT_FOUND_ROUTE_RE = re.compile(
    r"(?:^|[/#._-])(?:404|not[-_ ]?found)(?:[/#?._&=-]|$)",
    re.I,
)
_DETAIL_REQUEST_TYPES = {"document", "script", "xhr", "fetch"}
_DETAIL_CONTENT_SELECTORS = (
    "[class*='job-detail']", "[class*='job_detail']", "[class*='job-description']",
    "[class*='job_description']", "[class*='position-detail']", "[class*='position_detail']",
    "[class*='post-detail']", "[class*='post_detail']", "[class*='detail-content']",
    "[class*='detail_content']", "[class*='detail-body']", "[class*='detail_body']",
    "[class*='job-content']", "[class*='job_content']", "[class*='rich-text']",
    "[class*='rich_text']",
)


class _RenderBrowserSession:
    """Keep one browser for consecutive pages, with a fresh context per page."""

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._browser = None

    def browser(self, sync_playwright):
        if self._browser is None:
            try:
                playwright = self._stack.enter_context(sync_playwright())
                browser = launch_browser(
                    playwright,
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                self._stack.callback(browser.close)
                self._browser = browser
            except BaseException:
                self.close()
                raise
        return self._browser

    def close(self) -> None:
        try:
            self._stack.close()
        finally:
            self._stack = ExitStack()
            self._browser = None


_ACTIVE_RENDER_SESSION: ContextVar[_RenderBrowserSession | None] = ContextVar(
    "recruitops_render_browser_session", default=None,
)


@contextmanager
def company_render_session():
    """Bound browser reuse to one disposable company worker operation."""
    session = _RenderBrowserSession()
    token = _ACTIVE_RENDER_SESSION.set(session)
    try:
        yield session
    finally:
        try:
            session.close()
        finally:
            _ACTIVE_RENDER_SESSION.reset(token)


@contextmanager
def _borrow_render_browser(sync_playwright):
    session = _ACTIVE_RENDER_SESSION.get()
    if session is not None:
        yield session.browser(sync_playwright)
    else:
        temporary = _RenderBrowserSession()
        try:
            yield temporary.browser(sync_playwright)
        finally:
            temporary.close()


def _remaining_ms(deadline: float | None, fallback: int) -> int:
    if deadline is None:
        return max(1, fallback)
    return max(1, min(fallback, int((deadline - time.monotonic()) * 1_000)))


def _safe_metadata_url(value: Any) -> str:
    """Keep route evidence while removing credentials from the HTML metadata."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() == "data":
            return "data:"

        def clean_query(query: str) -> str:
            return urlencode([
                (key, item)
                for key, item in parse_qsl(query, keep_blank_values=True)
                if not _SENSITIVE_KEY_RE.search(key)
            ])

        fragment = parsed.fragment
        if "?" in fragment:
            fragment_path, _, fragment_query = fragment.partition("?")
            fragment = fragment_path
            cleaned = clean_query(fragment_query)
            if cleaned:
                fragment = f"{fragment}?{cleaned}"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query(parsed.query), fragment))[:2_048]
    except (TypeError, ValueError):
        return ""


def _classify_detail_route(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    route = f"{parsed.path}#{parsed.fragment}"
    if _DETAIL_LOGIN_ROUTE_RE.search(route):
        return "login_required"
    if _DETAIL_NOT_FOUND_ROUTE_RE.search(route):
        return "not_found"
    return ""


def _detail_dom_snapshot(page, title: str = "") -> dict[str, Any] | None:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        snapshot = evaluate(
            """target => {
                const visible = node => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
                };
                const textOf = node => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                const body = document.body ? textOf(document.body) : '';
                const selectors = target.detailSelectors;
                const detailNodes = selectors
                    .flatMap(selector => Array.from(document.querySelectorAll(selector)))
                    .filter((node, index, nodes) => nodes.indexOf(node) === index && visible(node));
                const detailText = detailNodes.map(textOf).filter(Boolean).join(' ');
                const loading = Array.from(document.querySelectorAll(
                    '[aria-busy="true"], [data-loading="true"], [role="progressbar"], '
                    '.loading, .is-loading, .skeleton, [class*="skeleton"]'
                )).some(visible) || /加载中|正在加载|loading|skeleton/i.test(body);
                const visibleInputs = Array.from(document.querySelectorAll('input')).filter(visible);
                const loginForm = visibleInputs.some(node =>
                    node.type === 'password' || /password|密码|手机号|mobile|phone/i.test(
                        `${node.name || ''} ${node.id || ''} ${node.placeholder || ''}`
                    )
                ) && /登录|验证码|sign\\s*in|log\\s*in/i.test(body);
                const notFound = /页面不存在|职位不存在|找不到(?:该|此)?页面|not found/i.test(body)
                    || /(?:^|\\D)404(?:\\D|$)/.test(document.title || '');
                const normalizedTitle = String(target.title || '').replace(/\\s+/g, '').toLowerCase();
                const normalizedBody = body.replace(/\\s+/g, '').toLowerCase();
                const titleMatch = Boolean(normalizedTitle && normalizedBody.includes(normalizedTitle));
                const semantic = /岗位职责|职位描述|工作内容|工作职责|任职要求|任职资格|招聘要求|职位详情|工作地点|招聘人数|responsibilit(?:y|ies)|requirements?|qualifications?/i.test(body);
                const detailSignal = detailText.length >= 40
                    || (semantic && (titleMatch || detailText.length >= 40) && body.length >= 120);
                return {
                    ready_state: document.readyState,
                    body_length: body.length,
                    detail_text_length: detailText.length,
                    detail_signal: detailSignal,
                    has_loading: loading,
                    login: loginForm,
                    not_found: notFound,
                    title_match: titleMatch,
                };
            }""",
            {"title": str(title or ""), "detailSelectors": list(_DETAIL_CONTENT_SELECTORS)},
        )
        return snapshot if isinstance(snapshot, dict) else None
    except Exception:
        return None


def _wait_for_detail_readiness(
    page,
    *,
    requested_url: str,
    deadline: float,
    detail_title: str = "",
    pending_requests: set[int] | None = None,
    failed_requests: Sequence[str] = (),
    document_status: int | None = None,
    navigation_state: str = "",
) -> dict[str, Any]:
    """Wait for a detail signal or an explicit terminal state within one deadline."""

    final_url = str(getattr(page, "url", "") or requested_url)
    route_state = _classify_detail_route(final_url)
    if route_state:
        return {
            "status": "incomplete",
            "method": "detail_page",
            "terminal_observed": False,
            "remaining_controls": [f"page_state:{route_state}"],
            "final_url": _safe_metadata_url(final_url),
            "load_state": route_state,
            "load_error": route_state,
        }
    if document_status == 404:
        return {
            "status": "incomplete",
            "method": "detail_page",
            "terminal_observed": False,
            "remaining_controls": ["page_state:not_found"],
            "final_url": _safe_metadata_url(final_url),
            "load_state": "not_found",
            "load_error": "http_404",
        }
    if not callable(getattr(page, "evaluate", None)):
        return {
            "status": "unknown",
            "method": "detail_page",
            "terminal_observed": False,
            "remaining_controls": [],
            "final_url": _safe_metadata_url(final_url),
            "load_state": "unknown",
            "load_error": "page_state_unavailable",
        }

    try:
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            wait_for_load_state("load", timeout=_remaining_ms(deadline, 5_000))
    except Exception:
        pass

    pending_requests = pending_requests if pending_requests is not None else set()
    consecutive_ready = 0
    last_snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        final_url = str(getattr(page, "url", "") or requested_url)
        route_state = _classify_detail_route(final_url)
        if route_state:
            return {
                "status": "incomplete",
                "method": "detail_page",
                "terminal_observed": False,
                "remaining_controls": [f"page_state:{route_state}"],
                "final_url": _safe_metadata_url(final_url),
                "load_state": route_state,
                "load_error": route_state,
            }
        if document_status == 404:
            return {
                "status": "incomplete",
                "method": "detail_page",
                "terminal_observed": False,
                "remaining_controls": ["page_state:not_found"],
                "final_url": _safe_metadata_url(final_url),
                "load_state": "not_found",
                "load_error": "http_404",
            }
        if failed_requests or navigation_state == "navigation_failed":
            return {
                "status": "incomplete",
                "method": "detail_page",
                "terminal_observed": False,
                "remaining_controls": ["request_failed"],
                "final_url": _safe_metadata_url(final_url),
                "load_state": "request_failed",
                "load_error": "request_failed",
            }
        snapshot = _detail_dom_snapshot(page, detail_title)
        if snapshot is not None:
            last_snapshot = snapshot
            if snapshot.get("login"):
                return {
                    "status": "incomplete",
                    "method": "detail_page",
                    "terminal_observed": False,
                    "remaining_controls": ["page_state:login_required"],
                    "final_url": _safe_metadata_url(final_url),
                    "load_state": "login_required",
                    "load_error": "login_required",
                }
            if snapshot.get("not_found"):
                return {
                    "status": "incomplete",
                    "method": "detail_page",
                    "terminal_observed": False,
                    "remaining_controls": ["page_state:not_found"],
                    "final_url": _safe_metadata_url(final_url),
                    "load_state": "not_found",
                    "load_error": "not_found",
                }
            ready = bool(snapshot.get("detail_signal")) and not snapshot.get("has_loading") and not pending_requests
            consecutive_ready = consecutive_ready + 1 if ready else 0
            if consecutive_ready >= 2:
                return {
                    "status": "complete",
                    "method": "detail_page",
                    "terminal_observed": True,
                    "remaining_controls": [],
                    "final_url": _safe_metadata_url(final_url),
                    "load_state": "ready",
                    "load_error": "",
                }
        try:
            page.wait_for_timeout(_remaining_ms(deadline, 200))
        except Exception:
            break

    final_url = str(getattr(page, "url", "") or requested_url)
    if failed_requests or navigation_state == "navigation_failed":
        load_state, load_error = "request_failed", "request_failed"
        controls = ["request_failed"]
    elif navigation_state == "navigation_timeout":
        load_state, load_error = "timeout", "navigation_timeout"
        controls = ["capture_timeout"]
    elif last_snapshot and last_snapshot.get("has_loading"):
        load_state, load_error = "incomplete", "page_loading"
        controls = ["page_loading"]
    elif pending_requests:
        load_state, load_error = "incomplete", "request_pending"
        controls = ["request_pending"]
    else:
        load_state, load_error = "incomplete", "detail_target_not_ready"
        controls = ["detail_target_not_ready"]
    return {
        "status": "incomplete",
        "method": "detail_page",
        "terminal_observed": False,
        "remaining_controls": controls,
        "final_url": _safe_metadata_url(final_url),
        "load_state": load_state,
        "load_error": load_error,
    }


def _redact_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "<depth-limit>"
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else _redact_json(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [_redact_json(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:2_000]
    return value


def _json_shape(value: Any) -> tuple[list[str], dict[str, int], dict[str, str]]:
    paths: list[str] = []
    arrays: dict[str, int] = {}
    samples: dict[str, str] = {}

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 8 or len(paths) >= 120:
            return
        paths.append(path)
        if isinstance(node, dict):
            for key, item in list(node.items())[:40]:
                if _SENSITIVE_KEY_RE.search(str(key)):
                    continue
                walk(item, f"{path}.{key}", depth + 1)
        elif isinstance(node, list):
            arrays[path] = len(node)
            if node:
                walk(node[0], f"{path}.0", depth + 1)
        elif node not in (None, "") and len(samples) < 40:
            samples[path] = str(node)[:160]

    walk(value, "$", 0)
    return paths, arrays, samples


def _read_stable_content(page, attempts: int = 3) -> str:
    """Read HTML after transient client-side redirects have settled."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return page.content()
        except Exception as exc:  # Playwright raises while a navigation is active.
            last_error = exc
            if attempt + 1 >= attempts:
                break
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(500)
    raise last_error or RuntimeError("无法读取渲染页面内容")


def _content_with_control_visibility(page) -> str:
    return page.evaluate("""() => {
        const clone = document.documentElement.cloneNode(true);
        const selector = 'input, iframe[src*="captcha"], form[action*="captcha"], #captcha, #challenge-form';
        const originals = document.documentElement.querySelectorAll(selector);
        clone.querySelectorAll(selector).forEach((node, index) => {
            const element = originals[index];
            const visible = element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
                && element.getClientRects().length > 0;
            node.setAttribute('data-recruitops-visible', String(visible));
        });
        return clone.outerHTML;
    }""")


def _valid_detail_interaction(recipe: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Accept only the finite, per-job detail interaction contract."""

    if not isinstance(recipe, Mapping):
        return None
    mode = str(recipe.get("mode") or "").strip().casefold()
    trigger_selector = str(recipe.get("trigger_selector") or "").strip()
    container_selector = str(recipe.get("container_selector") or "").strip()
    trigger_text = str(recipe.get("trigger_text") or "").strip()
    if mode not in _DETAIL_INTERACTION_MODES or not trigger_selector or not container_selector:
        return None
    if _DANGEROUS_CONTROL_RE.search(trigger_selector) or _DANGEROUS_CONTROL_RE.search(container_selector):
        return None
    tab_selectors = recipe.get("tab_selectors") or ()
    if mode == "tabs":
        if not isinstance(tab_selectors, (list, tuple)) or not tab_selectors:
            return None
        tab_selectors = tuple(
            str(selector).strip()
            for selector in tab_selectors
            if str(selector).strip() and not _DANGEROUS_CONTROL_RE.search(str(selector))
        )
        if not tab_selectors or len(tab_selectors) != len(recipe.get("tab_selectors") or ()):
            return None
    else:
        tab_selectors = ()
    return {
        "mode": mode,
        "trigger_selector": trigger_selector,
        "trigger_text": trigger_text,
        "container_selector": container_selector,
        "tab_selectors": tab_selectors,
    }


_TARGET_SCOPE_SCRIPT = """
(element, target) => {
  const normalize = value => String(value || '').normalize('NFKC').toLowerCase().replace(/\\s+/g, '');
  const title = normalize(target.title);
  const jobId = normalize(target.jobId);
  const idAttrs = ['data-job-id', 'data-position-id', 'data-post-id', 'data-jobid', 'data-positionid'];
  const titleAttrs = ['data-job-title', 'data-title', 'data-position-name', 'data-job-name'];
  const cardSelector = 'article,li,[data-job-id],[data-position-id],[data-post-id],.job-card,.job-item,.position-card,.ant-card';
  const titleSelectors = 'h1,h2,h3,h4,.job-title,.job-name,.position-title,.position-name,.post_name,.ant-drawer-title,.ant-modal-title,[itemprop="title"]';
  const ignored = /职位描述|岗位描述|职位职责|岗位职责|工作职责|任职要求|岗位要求|任职资格|招聘要求|job\\s*description|job\\s*requirements|responsibilities|duties|requirements|qualifications/i;
  const card = element.closest(cardSelector);
  if (!card) return {matched: false};
  const ids = idAttrs.map(attr => card.getAttribute(attr)).filter(Boolean).map(normalize);
  const titles = titleAttrs.map(attr => card.getAttribute(attr)).filter(Boolean);
  card.querySelectorAll(titleSelectors).forEach(node => {
    const value = String(node.textContent || '').trim();
    if (value && !ignored.test(value)) titles.push(value);
  });
  card.querySelectorAll('a[href]').forEach(link => {
    try {
      const parsed = new URL(link.href, document.baseURI);
      ['id', 'jobId', 'jobid', 'jobAdId', 'postId', 'positionId'].forEach(key => {
        if (parsed.searchParams.get(key)) ids.push(normalize(parsed.searchParams.get(key)));
      });
      const pathMatch = parsed.pathname.match(/\\/(?:job|jobs|position|posts)\\/([^/]+)/i);
      if (pathMatch) ids.push(normalize(decodeURIComponent(pathMatch[1])));
    } catch (_) {}
  });
  const uniqueIds = [...new Set(ids)];
  const uniqueTitles = [...new Set(titles.map(normalize).filter(Boolean))];
  const idMatches = !jobId || (uniqueIds.length > 0 && uniqueIds.every(value => value === jobId));
  const titleMatches = !title || (uniqueTitles.length > 0 && uniqueTitles.every(value => value === title));
  return {
    matched: idMatches && titleMatches && Boolean((jobId && uniqueIds.length) || (title && uniqueTitles.length)),
    candidateCount: idMatches && titleMatches ? 1 : 0,
  };
}
"""


_DETAIL_CONTAINER_SCRIPT = """
(element, target) => {
  const normalize = value => String(value || '').normalize('NFKC').toLowerCase().replace(/\\s+/g, '');
  const title = normalize(target.title);
  const jobId = normalize(target.jobId);
  const idAttrs = ['data-job-id', 'data-position-id', 'data-post-id', 'data-jobid', 'data-positionid'];
  const titleAttrs = ['data-job-title', 'data-title', 'data-position-name', 'data-job-name'];
  const titleSelectors = 'h1,h2,h3,h4,.job-title,.job-name,.position-title,.position-name,.post_name,.ant-drawer-title,.ant-modal-title,[itemprop="title"]';
  const ignored = /职位描述|岗位描述|职位职责|岗位职责|工作职责|任职要求|岗位要求|任职资格|招聘要求|job\\s*description|job\\s*requirements|responsibilities|duties|requirements|qualifications/i;
  const ids = idAttrs.map(attr => element.getAttribute(attr)).filter(Boolean).map(normalize);
  const titles = titleAttrs.map(attr => element.getAttribute(attr)).filter(Boolean);
  element.querySelectorAll(titleSelectors).forEach(node => {
    const value = String(node.textContent || '').trim();
    if (value && !ignored.test(value)) titles.push(value);
  });
  element.querySelectorAll('a[href]').forEach(link => {
    try {
      const parsed = new URL(link.href, document.baseURI);
      ['id', 'jobId', 'jobid', 'jobAdId', 'postId', 'positionId'].forEach(key => {
        if (parsed.searchParams.get(key)) ids.push(normalize(parsed.searchParams.get(key)));
      });
    } catch (_) {}
  });
  const uniqueIds = [...new Set(ids)];
  const uniqueTitles = [...new Set(titles.map(normalize).filter(Boolean))];
  const idMatches = !jobId || (uniqueIds.length > 0 && uniqueIds.every(value => value === jobId));
  const titleMatches = !title || (uniqueTitles.length > 0 && uniqueTitles.every(value => value === title));
  return {
    matched: idMatches && titleMatches && Boolean((jobId && uniqueIds.length) || (title && uniqueTitles.length)),
    text: (element.innerText || '').trim(),
  };
}
"""


def _resolve_unique_container(page, selector: str, target: Mapping[str, str], marker: str, deadline: float):
    """Resolve one identity-bound container without a page-wide first match."""

    scoped = page.locator(f'[data-recruitops-target-scope="{marker}"]').locator(selector)
    end = min(deadline, time.monotonic() + 5)
    while time.monotonic() < end:
        matches = []
        containers = page.locator(selector)
        for index in range(containers.count()):
            candidate = containers.nth(index)
            try:
                if candidate.is_visible() and candidate.evaluate(_DETAIL_CONTAINER_SCRIPT, target).get("matched"):
                    matches.append(candidate)
            except Exception:
                continue
        if len(matches) == 1:
            return matches[0], "matched"
        if len(matches) > 1:
            return None, "ambiguous"
        scope_root = page.locator(f'[data-recruitops-target-scope="{marker}"]')
        if scope_root.count() == 1:
            try:
                if scope_root.is_visible() and scope_root.evaluate(
                    "(element, value) => element.matches(value)", selector,
                ):
                    return scope_root, "scoped"
            except Exception:
                pass
        scoped_matches = []
        for index in range(scoped.count()):
            candidate = scoped.nth(index)
            try:
                if candidate.is_visible():
                    scoped_matches.append(candidate)
            except Exception:
                continue
        if len(scoped_matches) == 1:
            return scoped_matches[0], "scoped"
        if len(scoped_matches) > 1:
            return None, "ambiguous"
        page.wait_for_timeout(_remaining_ms(deadline, 100))
    return None, "unresolved"


def _wait_for_stable_container(page, container, deadline: float) -> tuple[bool, list[str]]:
    previous = None
    stable_reads = 0
    last_state: dict[str, Any] = {"text": "", "loading": False}
    end = min(deadline, time.monotonic() + 5)
    while time.monotonic() < end:
        try:
            last_state = container.evaluate(
                """element => {
                    const loading = element.matches('[aria-busy="true"],[data-loading="true"],[role="progressbar"]')
                        || Boolean(element.querySelector('[aria-busy="true"],[data-loading="true"],[role="progressbar"],.loading,.is-loading'))
                        || /加载中|正在加载|loading/i.test(element.innerText || '');
                    return {text: (element.innerText || '').trim(), loading};
                }"""
            )
        except Exception:
            return False, ["container_unreadable"]
        current = str(last_state.get("text") or "")
        if current and current == previous and not last_state.get("loading"):
            stable_reads += 1
        else:
            stable_reads = 0
        if stable_reads >= 2:
            return True, []
        previous = current
        page.wait_for_timeout(_remaining_ms(deadline, 150))
    return False, ["loading" if last_state.get("loading") else "content_unstable"]


def _run_detail_interaction(
    page,
    recipe: Mapping[str, Any] | None,
    *,
    title: str,
    job_id: str,
    deadline: float,
) -> dict[str, Any]:
    """Execute a bounded interaction and annotate the returned DOM with proof."""

    normalized = _valid_detail_interaction(recipe)
    if normalized is None or not (str(title).strip() or str(job_id).strip()):
        return {"status": "unknown", "method": "", "terminal_observed": False, "remaining_controls": []}

    target = {"title": str(title).strip(), "jobId": str(job_id).strip()}
    matches = []
    discovery_deadline = min(deadline, time.monotonic() + 8)
    while time.monotonic() < discovery_deadline:
        trigger = page.locator(normalized["trigger_selector"])
        matches = []
        for index in range(trigger.count()):
            candidate = trigger.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                label = candidate.get_attribute("aria-label") or candidate.get_attribute("title") or candidate.inner_text()
                if _DANGEROUS_CONTROL_RE.search(str(label or "")):
                    continue
                if normalized["trigger_text"] and " ".join(str(label or "").split()) != normalized["trigger_text"]:
                    continue
                identity = candidate.evaluate(_TARGET_SCOPE_SCRIPT, target)
                if identity.get("matched"):
                    matches.append(candidate)
            except Exception:
                continue
        if len(matches) > 1:
            return {
                "status": "unknown",
                "method": f"detail_interaction:{normalized['mode']}",
                "terminal_observed": False,
                "remaining_controls": ["ambiguous_target"],
            }
        if matches:
            break
        page.wait_for_timeout(_remaining_ms(discovery_deadline, 150))
    if not matches:
        return {"status": "unknown", "method": f"detail_interaction:{normalized['mode']}", "terminal_observed": False, "remaining_controls": []}
    matched = matches[0]

    marker = f"recruitops-target-{int(time.monotonic() * 1_000_000)}"
    try:
        matched.evaluate(
            """(element, marker) => {
                let current = element;
                for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                    if (current.matches('article,li,[data-job-id],[data-position-id],[data-post-id],.job-card,.job-item,.position-card,.ant-card')) {
                        current.setAttribute('data-recruitops-target-scope', marker);
                        return;
                    }
                }
                element.setAttribute('data-recruitops-target-scope', marker);
            }""",
            marker,
        )
        matched.click(timeout=_remaining_ms(deadline, 5000))
        container, container_status = _resolve_unique_container(
            page, normalized["container_selector"], target, marker, deadline,
        )
        if container is None:
            return {
                "status": "unknown",
                "method": f"detail_interaction:{normalized['mode']}",
                "terminal_observed": False,
                "remaining_controls": [f"container_{container_status}"],
            }
        stable, stability_controls = _wait_for_stable_container(page, container, deadline)
        if not stable:
            return {
                "status": "unknown",
                "method": f"detail_interaction:{normalized['mode']}",
                "terminal_observed": False,
                "remaining_controls": stability_controls,
            }
        snapshots = []
        if normalized["mode"] == "tabs":
            for selector in normalized["tab_selectors"]:
                tabs = container.locator(selector)
                if tabs.count() != 1:
                    return {
                        "status": "unknown",
                        "method": "detail_interaction:tabs",
                        "terminal_observed": False,
                        "remaining_controls": [f"tab_{selector}"],
                    }
                tab = tabs.first
                label = tab.get_attribute("aria-label") or tab.get_attribute("title") or tab.inner_text()
                if not tab.is_visible() or _DANGEROUS_CONTROL_RE.search(str(label or "")):
                    return {"status": "unknown", "method": "detail_interaction:tabs", "terminal_observed": False, "remaining_controls": [f"tab_{selector}"]}
                tab.click(timeout=_remaining_ms(deadline, 5000))
                container, container_status = _resolve_unique_container(
                    page, normalized["container_selector"], target, marker, deadline,
                )
                if container is None:
                    return {
                        "status": "unknown",
                        "method": "detail_interaction:tabs",
                        "terminal_observed": False,
                        "remaining_controls": [f"container_{container_status}"],
                    }
                stable, stability_controls = _wait_for_stable_container(page, container, deadline)
                if not stable:
                    return {
                        "status": "unknown",
                        "method": "detail_interaction:tabs",
                        "terminal_observed": False,
                        "remaining_controls": stability_controls,
                    }
                snapshots.append(container.inner_html())
            if snapshots:
                container.first.evaluate(
                    """(element, values) => {
                        const bucket = document.createElement('section');
                        bucket.setAttribute('data-recruitops-tab-captures', 'true');
                        values.forEach(value => {
                            const panel = document.createElement('div');
                            panel.setAttribute('data-recruitops-tab-panel', 'true');
                            panel.innerHTML = value;
                            bucket.appendChild(panel);
                        });
                        element.appendChild(bucket);
                    }""",
                    snapshots,
                )

        remaining = container.evaluate(
            """element => Array.from(element.querySelectorAll('button,a,input,select,[role="button"],[role="tab"]'))
                .filter(node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden')
                .map(node => (node.getAttribute('aria-label') || node.getAttribute('title') || node.innerText || '').trim())
                .filter(Boolean)
                .filter(value => /加载|展开|更多|查看详情|下一页|loading|load more|show more|continue/i.test(value))
                .filter((value, index, values) => values.indexOf(value) === index)
                .slice(0, 20)"""
        )
        if remaining:
            return {
                "status": "unknown",
                "method": f"detail_interaction:{normalized['mode']}",
                "terminal_observed": False,
                "remaining_controls": list(remaining),
            }
        container.evaluate(
            "element => element.setAttribute('data-recruitops-detail-container', 'true')"
        )
        return {
            "status": "complete",
            "method": f"detail_interaction:{normalized['mode']}",
            "terminal_observed": True,
            "remaining_controls": list(remaining or []),
        }
    except Exception:
        logger.debug("[render] 详情交互失败 %s", normalized, exc_info=True)
        return {
            "status": "incomplete",
            "method": f"detail_interaction:{normalized['mode']}",
            "terminal_observed": False,
            "remaining_controls": [],
        }
    finally:
        try:
            page.locator(f'[data-recruitops-target-scope="{marker}"]').evaluate(
                "element => element.removeAttribute('data-recruitops-target-scope')"
            )
        except Exception:
            pass


def _annotate_detail_capture(page, evidence: Mapping[str, Any]) -> None:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return
    try:
        evaluate(
            """evidence => {
                const root = document.documentElement;
                root.setAttribute('data-recruitops-capture-status', String(evidence.status || 'unknown'));
                root.setAttribute('data-recruitops-capture-method', String(evidence.method || ''));
                root.setAttribute('data-recruitops-terminal-observed', String(Boolean(evidence.terminal_observed)));
                root.setAttribute('data-recruitops-remaining-controls', JSON.stringify(evidence.remaining_controls || []));
                root.setAttribute('data-recruitops-final-url', String(evidence.final_url || ''));
                root.setAttribute('data-recruitops-load-state', String(evidence.load_state || 'unknown'));
                if (evidence.load_error) {
                    root.setAttribute('data-recruitops-load-error', String(evidence.load_error));
                } else {
                    root.removeAttribute('data-recruitops-load-error');
                }
            }""",
            dict(evidence),
        )
    except Exception:
        logger.debug("[render] 无法写入页面捕获元数据", exc_info=True)


def render_page(
    url: str,
    wait_for: Optional[str] = None,
    timeout_ms: int = 30000,
    extra_wait_ms: int = 0,
    scroll_times: int = 0,
    click_texts: Sequence[str] | None = None,
    click_selectors: Sequence[str] | None = None,
    detail_interaction: Mapping[str, Any] | None = None,
    detail_title: str = "",
    detail_job_id: str = "",
    annotate_visibility: bool = False,
    wait_until: Literal["domcontentloaded", "load", "networkidle"] | None = None,
) -> Optional[str]:
    """渲染 SPA 页面并返回完整 HTML。失败时返回 None。

    Args:
        url: 目标 URL
        wait_for: CSS selector，导航后等待该元素出现
        timeout_ms: 单次导航或元素等待的超时（毫秒）
        extra_wait_ms: selector 命中后额外等待的毫秒数（让懒加载完成）
        scroll_times: 额外滚动到底的次数（触发列表懒加载/分页加载）
        click_texts: 按顺序点击完全匹配的可见文本，用于展开筛选或行内详情
        click_selectors: 按顺序点击 CSS 选择器匹配的首个可见元素
        wait_until: 显式导航完成条件；未指定且有 wait_for 时先等 DOM 再等目标元素
    """
    deadline = time.monotonic() + max(1, timeout_ms) / 1000
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.error(
            "未安装 playwright。请运行：pip install -r requirements.txt && "
            "playwright install chromium"
        )
        return None

    try:
        with _borrow_render_browser(sync_playwright) as browser:
            context = browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="zh-CN",
                ignore_https_errors=True,
            )
            context.add_init_script(_STEALTH_INIT_SCRIPT)

            page = context.new_page()
            navigation_wait_until = wait_until or ("domcontentloaded" if wait_for else "networkidle")
            detail_mode = detail_interaction is not None or (
                navigation_wait_until == "domcontentloaded" and not wait_for and not scroll_times
            )
            pending_requests: set[int] = set()
            failed_requests: list[str] = []
            document_status: int | None = None
            navigation_state = ""

            def request_type(request) -> str:
                return str(getattr(request, "resource_type", "") or "").casefold()

            def track_request(request) -> bool:
                resource_type = request_type(request)
                if resource_type in _BLOCKED_RESOURCE_TYPES:
                    return False
                return resource_type in _DETAIL_REQUEST_TYPES

            def on_request(request) -> None:
                if track_request(request):
                    pending_requests.add(id(request))

            def on_request_finished(request) -> None:
                pending_requests.discard(id(request))

            def on_request_failed(request) -> None:
                pending_requests.discard(id(request))
                if track_request(request) and len(failed_requests) < 8:
                    failed_requests.append("request_failed")

            def on_response(response) -> None:
                nonlocal document_status
                request = getattr(response, "request", None)
                resource_type = request_type(request)
                try:
                    status = int(getattr(response, "status", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                if resource_type == "document":
                    document_status = status
                if (
                    status >= 400
                    and resource_type in _DETAIL_REQUEST_TYPES
                    and status != 404
                    and len(failed_requests) < 8
                ):
                    failed_requests.append("http_error")

            on = getattr(page, "on", None)
            if callable(on):
                for event, callback in (
                    ("request", on_request),
                    ("requestfinished", on_request_finished),
                    ("requestfailed", on_request_failed),
                    ("response", on_response),
                ):
                    try:
                        on(event, callback)
                    except Exception:
                        logger.debug("[render] 无法监听页面事件 %s", event, exc_info=True)
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                else route.continue_(),
            )

            try:
                try:
                    page.goto(
                        url,
                        wait_until=navigation_wait_until,
                        timeout=_remaining_ms(deadline, timeout_ms),
                    )
                except PWTimeout:
                    navigation_state = "navigation_timeout"
                    logger.warning("[render] goto %s 超时 %s（仍尝试解析当前页面）", navigation_wait_until, url)
                except Exception:
                    navigation_state = "navigation_failed"
                    logger.warning("[render] goto failed; keeping current page", exc_info=True)
                if wait_for:
                    try:
                        page.wait_for_selector(
                            wait_for,
                            timeout=_remaining_ms(deadline, timeout_ms),
                            state="attached",
                        )
                    except PWTimeout:
                        logger.warning("[render] selector %s 未在 %dms 内出现", wait_for, timeout_ms)
                if extra_wait_ms > 0 and not detail_mode:
                    page.wait_for_timeout(_remaining_ms(deadline, extra_wait_ms))
                for text in click_texts or ():
                    normalized = str(text or "").strip()
                    if not normalized or _DANGEROUS_CONTROL_RE.search(normalized):
                        continue
                    try:
                        target = page.get_by_text(normalized, exact=True).first
                        if target.count() and target.is_visible():
                            target.click(timeout=_remaining_ms(deadline, min(timeout_ms, 5000)))
                            page.wait_for_timeout(_remaining_ms(deadline, 500))
                    except Exception:
                        logger.debug("[render] 无法点击文本 %s", normalized, exc_info=True)
                for selector in click_selectors or ():
                    normalized = str(selector or "").strip()
                    if not normalized or _DANGEROUS_CONTROL_RE.search(normalized):
                        continue
                    try:
                        target = page.locator(normalized).first
                        if target.count() and target.is_visible():
                            target.click(timeout=_remaining_ms(deadline, min(timeout_ms, 5000)))
                            page.wait_for_timeout(_remaining_ms(deadline, 500))
                    except Exception:
                        logger.debug("[render] 无法点击选择器 %s", normalized, exc_info=True)
                if detail_interaction is not None:
                    interaction_evidence = _run_detail_interaction(
                        page,
                        detail_interaction,
                        title=detail_title,
                        job_id=detail_job_id,
                        deadline=deadline,
                    )
                    interaction_evidence = dict(interaction_evidence)
                    final_url = str(getattr(page, "url", "") or url)
                    route_state = _classify_detail_route(final_url)
                    if route_state:
                        interaction_evidence.update(
                            status="incomplete",
                            terminal_observed=False,
                            remaining_controls=[f"page_state:{route_state}"],
                            load_state=route_state,
                            load_error=route_state,
                        )
                    elif failed_requests or navigation_state == "navigation_failed":
                        interaction_evidence.update(
                            status="incomplete",
                            terminal_observed=False,
                            remaining_controls=["request_failed"],
                            load_state="request_failed",
                            load_error="request_failed",
                        )
                    elif interaction_evidence.get("status") == "complete":
                        interaction_evidence.update(load_state="ready", load_error="")
                    else:
                        interaction_evidence.update(load_state="incomplete", load_error="detail_target_not_ready")
                    interaction_evidence["final_url"] = _safe_metadata_url(final_url)
                    _annotate_detail_capture(page, interaction_evidence)
                elif detail_mode:
                    detail_evidence = _wait_for_detail_readiness(
                        page,
                        requested_url=url,
                        deadline=deadline,
                        detail_title=detail_title,
                        pending_requests=pending_requests,
                        failed_requests=failed_requests,
                        document_status=document_status,
                        navigation_state=navigation_state,
                    )
                    _annotate_detail_capture(page, detail_evidence)
                # 滚动到底加载懒加载列表（Moka/北森 等无限滚动列表）
                for _ in range(scroll_times):
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(_remaining_ms(deadline, 1200))
                    except Exception:
                        break
                if annotate_visibility:
                    return _content_with_control_visibility(page)
                return _read_stable_content(page)
            finally:
                context.close()
    except Exception as e:
        logger.error("[render] 渲染异常 %s: %s", url, e)
        return None


def observe_page_with_network(
    url: str,
    *,
    timeout_ms: int = 30000,
    extra_wait_ms: int = 2500,
    scroll_times: int = 3,
) -> dict[str, Any]:
    """Render a public page and capture bounded, redacted recruitment JSON evidence."""

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {"html": None, "responses": [], "error": "playwright_not_installed"}

    observations: list[dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            configured_budget = configured_crawl_timeout_seconds()
            deadline = (
                time.monotonic() + configured_budget
                if configured_budget is not None
                else None
            )
            browser = launch_browser(
                p,
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 768},
                locale="zh-CN",
                ignore_https_errors=True,
            )
            context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                else route.continue_(),
            )
            def collect(response) -> None:
                if len(observations) >= 20:
                    return
                try:
                    content_type = str(response.headers.get("content-type") or "").casefold()
                    if "json" not in content_type or not _RECRUITMENT_RESPONSE_RE.search(response.url):
                        return
                    payload = _redact_json(response.json())
                    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    paths, arrays, samples = _json_shape(payload)
                    request_body = response.request.post_data_json if response.request.post_data else None
                    observations.append({
                        "url": response.url[:2_048],
                        "method": response.request.method,
                        "status": int(response.status),
                        "content_type": content_type[:120],
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "paths": paths,
                        "array_paths": arrays,
                        "scalar_samples": samples,
                        "request_body": _redact_json(request_body),
                        "payload": payload,
                    })
                except Exception:
                    return

            page.on("response", collect)
            try:
                navigation_state = ""
                try:
                    page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=_remaining_ms(deadline, timeout_ms),
                    )
                except PWTimeout:
                    logger.warning("[observe] goto networkidle timeout %s", url)
                if extra_wait_ms:
                    page.wait_for_timeout(_remaining_ms(deadline, extra_wait_ms))
                for _ in range(scroll_times):
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(_remaining_ms(deadline, 900))
                    except Exception:
                        break
                html = _read_stable_content(page)
                return {"html": html, "responses": observations, "final_url": page.url}
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        logger.error("[observe] page/network observation failed %s: %s", url, exc)
        return {"html": None, "responses": observations, "error": str(exc)[-500:]}
