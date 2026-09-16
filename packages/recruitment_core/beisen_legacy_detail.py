"""Standalone parser for legacy Beisen ``/zpdetail/<id>`` pages.

Legacy Beisen tenants render a server-side detail page while newer tenants use
the ``jobAdId`` API/detail route.  This module intentionally only parses the
legacy page DOM.  It has no crawler, database, scoring, or hydration
dependency so the core path can choose it as a narrow fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


SOURCE = "beisen_legacy_detail_dom"

_LEGACY_ROUTE_RE = re.compile(r"/zpdetail/(?P<job_id>\d+)(?:/|$)", re.IGNORECASE)
_MODERN_JOB_AD_RE = re.compile(r"(?:^|[?&#])jobAdId=", re.IGNORECASE)

_SECTION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "responsibilities",
        (
            "岗位职责",
            "工作职责",
            "职位职责",
            "工作内容",
            "岗位描述",
            "职位描述",
            "responsibilities",
            "job responsibilities",
        ),
    ),
    (
        "requirements",
        (
            "任职资格",
            "任职要求",
            "岗位要求",
            "职位要求",
            "岗位条件",
            "任职条件",
            "资格要求",
            "requirements",
            "qualifications",
        ),
    ),
)
_SECTION_ALIAS_TO_KIND = {
    alias.casefold(): kind
    for kind, aliases in _SECTION_ALIASES
    for alias in aliases
}
_SECTION_ALIASES_SORTED = tuple(
    sorted(
        ((alias, kind) for kind, aliases in _SECTION_ALIASES for alias in aliases),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_APPLICATION_LABEL_RE = re.compile(
    r"[\(（]\s*(?P<label>未申请|已申请|未投递|已投递)\s*[\)）]",
    re.IGNORECASE,
)
_APPLICATION_ONLY_RE = re.compile(r"^(?:未申请|已申请|未投递|已投递)$", re.IGNORECASE)

_RECOMMENDATION_LABEL_RE = re.compile(
    r"^(?:更多\s*)?(?:热招|热门|长招|推荐|相关|相似)\s*"
    r"(?:职位|岗位|position|job)(?:\s*(?:更多|列表|推荐))?"
    r"(?:\s*[>＞]+)?\s*[:：]?$",
    re.IGNORECASE,
)
_BODY_STOP_RE = re.compile(
    r"^(?:现在|立即|马上)?申请(?:职位|岗位)?$|"
    r"^(?:返回职位列表|返回列表|收藏|分享|举报|登录|注册)$|"
    r"^©|^copyright\b",
    re.IGNORECASE,
)
_GENERIC_TITLE_KEYS = {
    "招聘详细",
    "招聘详情",
    "职位详情",
    "岗位详情",
    "校园招聘",
    "社会招聘",
    "实习生招聘",
    "首页",
}
_TITLE_ATTR_RE = re.compile(
    r"(?:stjobtitle|job[-_ ]?title|position[-_ ]?title|post[-_ ]?title|"
    r"detail[-_ ]?title|jobname|positionname|postname|data-job-title)",
    re.IGNORECASE,
)
_DETAIL_ATTR_RE = re.compile(
    r"(?:stjobdetail|stjobcontent|job[-_ ]?(?:detail|description|content)|"
    r"position[-_ ]?(?:detail|description|content)|post[-_ ]?(?:detail|description|content)|"
    r"detail[-_ ]?(?:content|body|description)|description[-_ ]?content)",
    re.IGNORECASE,
)
_RECOMMENDATION_ATTR_RE = re.compile(
    r"(?:recommend|related|similar|popular|hot(?:[-_ ]?(?:job|position|list))?|"
    r"long(?:[-_ ]?(?:term|job|position))?|sidebar|side[-_]?bar|"
    r"right(?:[-_ ]?(?:side|panel|column|bar))?|热招|热门|推荐|相关|相似|长招)",
    re.IGNORECASE,
)
_IDENTITY_ATTR_RE = re.compile(
    r"(?:job|jobad|position|post)[-_ ]?(?:ad[-_ ]?)?id|"
    r"id[-_ ]?(?:job|jobad|position|post)",
    re.IGNORECASE,
)
_NOISE_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "template",
    "iframe",
    "button",
    "input",
    "select",
    "textarea",
}


@dataclass(frozen=True, slots=True)
class BeisenLegacyDetailResult:
    """Neutral result consumed by the core adapter.

    ``body_evidence`` is deliberately structured rather than reduced to a
    character-count diagnostic.  Each entry retains the complete text for one
    observed detail section and the DOM descriptor that supplied it.
    """

    status: str
    job_id: str = ""
    title: str = ""
    detail_url: str = ""
    body: str = ""
    body_evidence: tuple[dict[str, object], ...] = ()
    application_label: str = ""
    identity_status: str = "unverified"
    identity_evidence: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    source: str = SOURCE

    @property
    def detail(self) -> str:
        """Alias matching the surrounding hydration vocabulary."""

        return self.body

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def identity_verified(self) -> bool:
        return self.identity_status in {"matched", "request_bound"}

    @property
    def request_result(self) -> str:
        """Expose the parse outcome without implying an HTTP request occurred."""

        return self.status

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly copy for capture diagnostics."""

        return {
            "status": self.status,
            "job_id": self.job_id,
            "title": self.title,
            "detail_url": self.detail_url,
            "body": self.body,
            "body_evidence": [_json_value(item) for item in self.body_evidence],
            "application_label": self.application_label,
            "identity_status": self.identity_status,
            "identity_evidence": list(self.identity_evidence),
            "diagnostics": _json_value(dict(self.diagnostics)),
            "source": self.source,
        }


def is_beisen_legacy_detail_url(url: str) -> bool:
    """Return whether *url* is the legacy ``/zpdetail/<numeric-id>`` route."""

    parsed = urlparse(str(url or "").strip())
    return bool(_LEGACY_ROUTE_RE.search(parsed.path))


def beisen_legacy_route_id(url: str) -> str:
    """Extract the numeric ID owned by a legacy detail route."""

    parsed = urlparse(str(url or "").strip())
    match = _LEGACY_ROUTE_RE.search(parsed.path)
    return match.group("job_id") if match else ""


def parse_beisen_legacy_detail(
    html: str,
    *,
    url: str = "",
    expected_title: str = "",
    expected_job_id: str = "",
) -> BeisenLegacyDetailResult:
    """Parse one legacy Beisen detail page without fetching or mutating state.

    The URL is the request binding.  A modern ``jobAdId`` URL is explicitly
    ``not_applicable`` so this parser can coexist with the API-based adapter.
    Missing identity observations are reported as ``identity_unverified``;
    only a positively observed conflicting ID/title is ``identity_mismatch``.
    """

    detail_url = str(url or "").strip()
    route_id = beisen_legacy_route_id(detail_url)
    if not route_id:
        if _MODERN_JOB_AD_RE.search(urlparse(detail_url).query):
            return _result(
                "not_applicable",
                detail_url=detail_url,
                diagnostics={"route_kind": "modern_jobAdId", "observation_status": "not_applicable"},
            )
        return _result(
            "not_applicable",
            detail_url=detail_url,
            diagnostics={"route_kind": "not_legacy_zpdetail", "observation_status": "not_applicable"},
        )

    soup = BeautifulSoup(html or "", "html.parser")
    title_candidates = _find_title_candidates(soup, expected_title)
    title_node = title_candidates[0][0] if title_candidates else None
    title = title_candidates[0][1] if title_candidates else ""
    application_label = title_candidates[0][2] if title_candidates else ""

    section_nodes = _find_section_nodes(soup)
    pending_controls = _pending_capture_controls(soup)
    scope = _find_primary_scope(soup, title_node, [item[2] for item in section_nodes])
    if scope is None:
        scope = soup.body or soup

    _remove_noise(scope, protected=(title_node, *(item[2] for item in section_nodes)))

    # A title may be outside the selected body root (some legacy layouts put
    # it in a sibling header).  Re-read title candidates after cleaning the
    # selected root, but keep the first observed title as a fallback.
    cleaned_candidates = _find_title_candidates(scope, expected_title)
    observed_titles = _unique_titles([item[1] for item in cleaned_candidates])
    if title and _comparison_key(title) not in {_comparison_key(item) for item in observed_titles}:
        observed_titles.insert(0, title)
    if cleaned_candidates:
        chosen = _choose_title_candidate(cleaned_candidates, expected_title)
        title = chosen[1]
        application_label = application_label or chosen[2]

    if not application_label:
        application_label = _find_application_label(title, title_node, scope)

    observed_job_ids = _observed_job_ids(scope)
    body, body_evidence, section_diagnostics = _extract_body(scope, section_nodes)
    identity_status, identity_evidence, identity_reason = _check_identity(
        route_id,
        expected_job_id=expected_job_id,
        expected_title=expected_title,
        observed_job_ids=observed_job_ids,
        observed_titles=observed_titles,
        title=title,
    )

    diagnostics: dict[str, object] = {
        "route_kind": "legacy_zpdetail",
        "route_job_id": route_id,
        "expected_job_id": _clean_id(expected_job_id),
        "observed_job_ids": list(observed_job_ids),
        "observed_titles": list(observed_titles),
        "observation_status": identity_status,
        "primary_scope": _node_descriptor(scope),
        "recommendation_blocks_removed": int(section_diagnostics.pop("recommendation_blocks_removed", 0)),
        "application_label": application_label,
        "pending_controls": pending_controls,
        **section_diagnostics,
    }

    if identity_status == "mismatch":
        return _result(
            "identity_mismatch",
            job_id=route_id,
            title=title,
            detail_url=detail_url,
            body="",
            body_evidence=body_evidence,
            application_label=application_label,
            identity_status=identity_status,
            identity_evidence=identity_evidence + identity_reason,
            diagnostics=diagnostics,
        )

    if not body:
        return _result(
            "body_missing",
            job_id=route_id,
            title=title,
            detail_url=detail_url,
            body_evidence=body_evidence,
            application_label=application_label,
            identity_status=identity_status,
            identity_evidence=identity_evidence + identity_reason,
            diagnostics=diagnostics,
        )

    if pending_controls:
        return _result(
            "not_ready",
            job_id=route_id,
            title=title,
            detail_url=detail_url,
            body=body,
            body_evidence=body_evidence,
            application_label=application_label,
            identity_status=identity_status,
            identity_evidence=identity_evidence + identity_reason,
            diagnostics=diagnostics,
        )

    if identity_status == "unverified":
        return _result(
            "identity_unverified",
            job_id=route_id,
            title=title,
            detail_url=detail_url,
            body=body,
            body_evidence=body_evidence,
            application_label=application_label,
            identity_status=identity_status,
            identity_evidence=identity_evidence + identity_reason,
            diagnostics=diagnostics,
        )

    return _result(
        "complete",
        job_id=route_id,
        title=title,
        detail_url=detail_url,
        body=body,
        body_evidence=body_evidence,
        application_label=application_label,
        identity_status=identity_status,
        identity_evidence=identity_evidence + identity_reason,
        diagnostics=diagnostics,
    )


# Explicit alias for callers that name the platform before the route shape.
parse_legacy_beisen_detail = parse_beisen_legacy_detail


def _result(
    status: str,
    *,
    job_id: str = "",
    title: str = "",
    detail_url: str = "",
    body: str = "",
    body_evidence: tuple[dict[str, object], ...] = (),
    application_label: str = "",
    identity_status: str = "unverified",
    identity_evidence: tuple[str, ...] = (),
    diagnostics: Mapping[str, object] | None = None,
) -> BeisenLegacyDetailResult:
    return BeisenLegacyDetailResult(
        status=status,
        job_id=job_id,
        title=title,
        detail_url=detail_url,
        body=body,
        body_evidence=body_evidence,
        application_label=application_label,
        identity_status=identity_status,
        identity_evidence=identity_evidence,
        diagnostics=dict(diagnostics or {}),
    )


def _pending_capture_controls(root: Any) -> tuple[str, ...]:
    """Report controls that indicate a rendered detail is not final yet."""

    found: list[str] = []
    for node in root.find_all(True):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text or len(text) > 80:
            continue
        marker = text.casefold()
        # A container repeats descendant text; only report the smallest
        # matching node so diagnostics identify the actual pending control.
        descendant_text = " ".join(
            " ".join(child.get_text(" ", strip=True).split())
            for child in node.find_all(True)
        ).casefold()
        if descendant_text and any(token in descendant_text for token in ("加载中", "正在加载", "展开更多", "查看全部", "加载更多")):
            continue
        if any(token in marker for token in ("加载中", "正在加载", "展开更多", "查看全部", "加载更多")):
            found.append(text)
    return tuple(dict.fromkeys(found))[:8]


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _clean_text(value: object) -> str:
    text = unescape(str(value or "")).replace("\xa0", " ")
    return " ".join(text.split()).strip()


def _comparison_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value))
    text, _ = _strip_application_label(text)
    return text.casefold()


def _clean_id(value: object) -> str:
    return _clean_text(value)


def _strip_application_label(value: object) -> tuple[str, str]:
    text = _clean_text(value)
    labels: list[str] = []

    def replace(match: re.Match[str]) -> str:
        labels.append(match.group("label"))
        return " "

    text = _APPLICATION_LABEL_RE.sub(replace, text)
    if not labels and _APPLICATION_ONLY_RE.fullmatch(text):
        labels.append(text)
        text = ""
    return _clean_text(text), (labels[0] if labels else "")


def _tag_attr_text(tag: Any) -> str:
    parts: list[str] = []
    for key in ("id", "class", "role", "data-section", "data-testid", "aria-label"):
        value = tag.get(key) if hasattr(tag, "get") else None
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _is_hidden(tag: Any) -> bool:
    if not hasattr(tag, "get"):
        return False
    if tag.has_attr("hidden") or str(tag.get("aria-hidden") or "").casefold() == "true":
        return True
    style = str(tag.get("style") or "")
    return bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.IGNORECASE))


def _is_recommendation_hint(tag: Any) -> bool:
    return bool(_RECOMMENDATION_ATTR_RE.search(_tag_attr_text(tag)))


def _inside_recommendation_hint(tag: Any) -> bool:
    current = tag
    while current is not None:
        if _is_recommendation_hint(current):
            return True
        current = getattr(current, "parent", None)
    return False


def _is_recommendation_label(value: object) -> bool:
    return bool(_RECOMMENDATION_LABEL_RE.fullmatch(_clean_text(value)))


def _is_body_stop(value: object) -> bool:
    return bool(_BODY_STOP_RE.match(_clean_text(value))) or _is_recommendation_label(value)


def _parse_section_line(value: object) -> tuple[str, str, str] | None:
    text = _clean_text(value)
    if not text:
        return None
    for alias, kind in _SECTION_ALIASES_SORTED:
        if text.casefold() == alias.casefold():
            return kind, alias, ""
        match = re.match(
            rf"^{re.escape(alias)}(?:\s*[：:]\s*(?P<colon_rest>.*)|\s+(?P<space_rest>.+))$",
            text,
            re.IGNORECASE,
        )
        if match and len(text) <= 240:
            return kind, alias, _clean_text(match.group("colon_rest") or match.group("space_rest"))
    return None


def _node_descriptor(node: Any) -> str:
    if node is None:
        return ""
    name = str(getattr(node, "name", "node") or "node")
    if name == "[document]":
        name = "document"
    node_id = str(node.get("id") or "").strip() if hasattr(node, "get") else ""
    classes = node.get("class") if hasattr(node, "get") else []
    if isinstance(classes, str):
        classes = classes.split()
    class_text = ".".join(str(item).strip() for item in (classes or []) if str(item).strip())
    descriptor = name
    if node_id:
        descriptor += f"#{node_id}"
    if class_text:
        descriptor += f".{class_text}"
    return descriptor


def _title_candidate(tag: Any, expected_title: str) -> tuple[int, str, str] | None:
    if getattr(tag, "name", "") in {"script", "style", "noscript", "svg", "title"}:
        return None
    if _is_hidden(tag) or _inside_recommendation_hint(tag):
        return None
    data_title = tag.get("data-job-title") if hasattr(tag, "get") else ""
    raw = _clean_text(data_title or tag.get_text(" ", strip=True))
    title, application_label = _strip_application_label(raw)
    if not title or len(title) > 220:
        return None
    key = _comparison_key(title)
    if not key or key in _GENERIC_TITLE_KEYS:
        return None
    if _parse_section_line(title) or _is_recommendation_label(title) or _is_body_stop(title):
        return None
    attr_hint = bool(data_title) or bool(_TITLE_ATTR_RE.search(_tag_attr_text(tag)))
    heading_score = {"h1": 700, "h2": 560, "h3": 450, "h4": 360}.get(tag.name, 0)
    expected_key = _comparison_key(expected_title)
    expected_match = bool(expected_key and key == expected_key)
    if not attr_hint and not heading_score and not expected_match:
        return None
    score = heading_score + (1200 if attr_hint else 0)
    if expected_match:
        score += 10000
    if hasattr(tag, "get") and "/zpdetail/" in str(tag.get("href") or "").casefold():
        score -= 800
    if not application_label:
        parent = getattr(tag, "parent", None)
        parent_text = _clean_text(parent.get_text(" ", strip=True)) if parent else ""
        if len(parent_text) <= 600:
            _, application_label = _strip_application_label(parent_text)
    return score, title, application_label


def _find_title_candidates(
    root: Any,
    expected_title: str,
) -> list[tuple[Any, str, str]]:
    candidates: list[tuple[int, Any, str, str]] = []
    for tag in root.find_all(True):
        parsed = _title_candidate(tag, expected_title)
        if parsed is None:
            continue
        score, title, application_label = parsed
        # Prefer the smallest meaningful element when a parent and child have
        # the same visible title text.
        text_len = len(title)
        score -= text_len
        candidates.append((score, tag, title, application_label))
    candidates.sort(key=lambda item: item[0], reverse=True)
    unique: set[str] = set()
    result: list[tuple[Any, str, str]] = []
    for _, tag, title, application_label in candidates:
        key = _comparison_key(title)
        if key in unique:
            continue
        unique.add(key)
        result.append((tag, title, application_label))
    return result


def _choose_title_candidate(
    candidates: list[tuple[Any, str, str]],
    expected_title: str,
) -> tuple[Any, str, str]:
    expected_key = _comparison_key(expected_title)
    if expected_key:
        for candidate in candidates:
            if _comparison_key(candidate[1]) == expected_key:
                return candidate
    return candidates[0]


def _unique_titles(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        title = _clean_text(value)
        key = _comparison_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(title)
    return result


def _find_section_nodes(root: Any) -> list[tuple[str, str, Any]]:
    candidates: list[tuple[str, str, Any]] = []
    for tag in root.find_all(True):
        if getattr(tag, "name", "") in {"script", "style", "noscript", "svg", "title"}:
            continue
        if _is_hidden(tag) or _inside_recommendation_hint(tag):
            continue
        parsed = _parse_section_line(tag.get_text(" ", strip=True))
        if parsed is None:
            continue
        kind, label, _ = parsed
        candidates.append((kind, label, tag))

    selected: list[tuple[str, str, Any]] = []
    candidate_ids = {id(tag) for _, _, tag in candidates}
    for kind, label, tag in candidates:
        if any(
            id(descendant) in candidate_ids and _parse_section_line(descendant.get_text(" ", strip=True))
            for descendant in tag.find_all(True)
        ):
            continue
        selected.append((kind, label, tag))
    return selected


def _contains(ancestor: Any, descendant: Any) -> bool:
    if ancestor is None or descendant is None:
        return False
    current = descendant
    while current is not None:
        if current is ancestor:
            return True
        current = getattr(current, "parent", None)
    return False


def _rough_text_length(node: Any) -> int:
    if node is None:
        return 0
    return len(_clean_text(node.get_text(" ", strip=True)))


def _find_primary_scope(root: Any, title_node: Any, section_nodes: list[Any]) -> Any:
    targets = [node for node in (title_node, *section_nodes) if node is not None]
    candidates: list[tuple[int, int, Any]] = []
    for target in targets:
        current = target
        while current is not None:
            name = getattr(current, "name", "")
            if name in {"html", "head", "script", "style", "noscript"}:
                current = getattr(current, "parent", None)
                continue
            marker_count = sum(1 for node in section_nodes if _contains(current, node))
            if marker_count:
                has_title = bool(title_node and _contains(current, title_node))
                attr_text = _tag_attr_text(current)
                score = marker_count * 1800 + (900 if has_title else 0)
                if _DETAIL_ATTR_RE.search(attr_text):
                    score += 500
                if name in {"main", "article", "section"}:
                    score += 120
                if _is_recommendation_hint(current):
                    score -= 1600
                text_length = _rough_text_length(current)
                score -= min(text_length // 25, 700)
                candidates.append((score, -text_length, current))
            current = getattr(current, "parent", None)
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    # A fallback detail class is safer than the whole document when the page
    # uses non-standard section labels.
    detail_candidates = [
        tag
        for tag in root.find_all(True)
        if _DETAIL_ATTR_RE.search(_tag_attr_text(tag))
        and not _inside_recommendation_hint(tag)
        and _rough_text_length(tag) > 0
    ]
    if detail_candidates:
        return min(detail_candidates, key=_rough_text_length)
    return root.body or root


def _recommendation_block(node: Any, root: Any) -> Any:
    current = node
    while current is not None and current is not root:
        if getattr(current, "name", "") == "aside" or _is_recommendation_hint(current):
            return current
        current = getattr(current, "parent", None)

    current = node
    while current is not None and current is not root:
        text_length = _rough_text_length(current)
        has_detail_link = bool(current.select("a[href*='/zpdetail/']"))
        has_section = bool(_find_section_nodes(current))
        if has_detail_link and not has_section and text_length <= 16000:
            return current
        current = getattr(current, "parent", None)
    return node


def _recommendation_blocks(root: Any) -> list[Any]:
    blocks: list[Any] = []
    seen: set[int] = set()
    for tag in root.find_all(True):
        text = _clean_text(tag.get_text(" ", strip=True))
        is_label = len(text) <= 120 and _is_recommendation_label(text)
        is_hint = _is_recommendation_hint(tag) or getattr(tag, "name", "") == "aside"
        if not (is_label or is_hint):
            continue
        block = _recommendation_block(tag, root) if is_label else tag
        if block is root or block is None or id(block) in seen:
            continue
        seen.add(id(block))
        blocks.append(block)
    return blocks


def _remove_noise(root: Any, protected: tuple[Any, ...]) -> None:
    protected_nodes = tuple(node for node in protected if node is not None)
    blocks = _recommendation_blocks(root)
    removed_blocks = 0
    for block in blocks:
        if block is root or block.parent is None:
            continue
        if any(_contains(block, node) for node in protected_nodes):
            continue
        block.decompose()
        removed_blocks += 1

    for tag in list(root.find_all(True)):
        if tag.parent is None:
            continue
        name = getattr(tag, "name", "")
        role = str(tag.get("role") or "").casefold()
        if name in _NOISE_TAGS or name in {"header", "nav", "footer"}:
            if any(_contains(tag, node) for node in protected_nodes):
                continue
            tag.decompose()
        elif _is_hidden(tag) or role in {"button", "tab"}:
            if any(_contains(tag, node) for node in protected_nodes):
                continue
            tag.decompose()
    # Keep a diagnostic on the root without introducing a second result type.
    setattr(root, "_beisen_recommendation_blocks_removed", removed_blocks)


def _visible_lines(root: Any) -> list[str]:
    raw_text = root.get_text("\n", strip=True) if root is not None else ""
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line, _ = _strip_application_label(raw_line)
        if not line or _APPLICATION_ONLY_RE.fullmatch(line):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def _extract_body(
    root: Any,
    original_section_nodes: list[tuple[str, str, Any]],
) -> tuple[str, tuple[dict[str, object], ...], dict[str, object]]:
    lines = _visible_lines(root)
    marker_lines: list[tuple[int, str, str, str]] = []
    for index, line in enumerate(lines):
        parsed = _parse_section_line(line)
        if parsed is not None:
            kind, label, rest = parsed
            marker_lines.append((index, kind, label, rest))

    scope_descriptor = _node_descriptor(root)
    diagnostics: dict[str, object] = {
        "line_count": len(lines),
        "section_markers": [label for _, _, label, _ in marker_lines],
        "recommendation_blocks_removed": int(getattr(root, "_beisen_recommendation_blocks_removed", 0)),
    }
    if not marker_lines:
        # The old templates sometimes omit labels and expose one bounded
        # ``job-detail`` block.  Accept its complete text without applying a
        # length threshold; callers decide policy outside this parser.
        title_keys = {
            _comparison_key(item[1])
            for item in _find_title_candidates(root, "")
            if _comparison_key(item[1])
        }
        body_lines = [
            line
            for line in lines
            if (
                not _is_body_stop(line)
                and not _APPLICATION_ONLY_RE.fullmatch(line)
                and _comparison_key(line) not in title_keys
            )
        ]
        if body_lines:
            body = "\n".join(body_lines).strip()
            evidence = (
                {
                    "section": "body",
                    "label": "",
                    "text": body,
                    "lines": tuple(body_lines),
                    "selector": scope_descriptor,
                    "source": SOURCE,
                },
            )
            diagnostics["body_mode"] = "bounded_detail_fallback"
            return body, evidence, diagnostics
        diagnostics["body_mode"] = "missing"
        return "", (), diagnostics

    start = marker_lines[0][0]
    end = len(lines)
    for index, line in enumerate(lines[start + 1 :], start=start + 1):
        if _is_body_stop(line):
            end = index
            break

    selected_lines = lines[start:end]
    content_exists = False
    body_evidence: list[dict[str, object]] = []
    for position, (index, kind, label, inline_rest) in enumerate(marker_lines):
        if index < start or index >= end:
            continue
        section_end = end
        if position + 1 < len(marker_lines):
            next_index = marker_lines[position + 1][0]
            if next_index < end:
                section_end = next_index
        content: list[str] = []
        if inline_rest:
            content.append(inline_rest)
        content.extend(lines[index + 1 : section_end])
        content = [line for line in content if line and not _is_body_stop(line)]
        content_text = "\n".join(content).strip()
        if content_text:
            content_exists = True
        body_evidence.append(
            {
                "section": kind,
                "label": label,
                "text": content_text,
                "lines": tuple(content),
                "selector": _section_selector(original_section_nodes, kind, label, scope_descriptor),
                "source": SOURCE,
            }
        )

    if not content_exists:
        diagnostics["body_mode"] = "labeled_empty"
        return "", tuple(body_evidence), diagnostics

    body_lines = [line for line in selected_lines if line and not _is_body_stop(line)]
    body = "\n".join(body_lines).strip()
    diagnostics["body_mode"] = "labeled_sections"
    diagnostics["body_char_count"] = len(body)
    return body, tuple(body_evidence), diagnostics


def _section_selector(
    nodes: list[tuple[str, str, Any]],
    kind: str,
    label: str,
    fallback: str,
) -> str:
    for node_kind, node_label, node in nodes:
        if node_kind == kind and node_label.casefold() == label.casefold():
            return _node_descriptor(node)
    return fallback


def _observed_job_ids(root: Any) -> tuple[str, ...]:
    observed: set[str] = set()
    for tag in root.find_all(True):
        for key, value in getattr(tag, "attrs", {}).items():
            attr_name = str(key).casefold()
            raw_value = unescape(str(value or ""))
            route_matches = _LEGACY_ROUTE_RE.findall(raw_value)
            observed.update(route_matches)
            if _IDENTITY_ATTR_RE.search(attr_name):
                if raw_value.strip().isdigit():
                    observed.add(raw_value.strip())
                else:
                    observed.update(re.findall(r"(?<!\d)\d{4,}(?!\d)", raw_value))
            elif attr_name == "id" and re.search(r"(?:job|position|post|ad)", raw_value, re.IGNORECASE):
                observed.update(re.findall(r"(?<!\d)\d{4,}(?!\d)", raw_value))
    return tuple(sorted(observed))


def _find_application_label(title: str, title_node: Any, scope: Any) -> str:
    for value in (title, title_node.get_text(" ", strip=True) if title_node else ""):
        _, label = _strip_application_label(value)
        if label:
            return label
    if scope is not None:
        for line in _visible_lines(scope):
            _, label = _strip_application_label(line)
            if label:
                return label
    return ""


def _check_identity(
    route_id: str,
    *,
    expected_job_id: str,
    expected_title: str,
    observed_job_ids: tuple[str, ...],
    observed_titles: list[str],
    title: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    evidence: list[str] = [f"route_id:{route_id}"]
    reasons: list[str] = []
    expected_id = _clean_id(expected_job_id)
    if expected_id:
        evidence.append(f"expected_job_id:{expected_id}")
        if expected_id != route_id:
            reasons.append("reason:route_id_mismatch")

    for observed_id in observed_job_ids:
        evidence.append(f"observed_job_id:{observed_id}")
    if observed_job_ids and any(observed_id != route_id for observed_id in observed_job_ids):
        reasons.append("reason:page_job_id_mismatch")

    for observed_title in observed_titles:
        evidence.append(f"observed_title:{observed_title}")
    expected_title_key = _comparison_key(expected_title)
    observed_title_keys = {_comparison_key(value) for value in observed_titles if _comparison_key(value)}
    if expected_title_key:
        evidence.append(f"expected_title:{_clean_text(expected_title)}")
        if not observed_title_keys:
            identity_status = "unverified"
        elif expected_title_key not in observed_title_keys:
            reasons.append("reason:title_mismatch")
            identity_status = "mismatch"
        elif len(observed_title_keys) > 1:
            reasons.append("reason:multiple_primary_titles")
            identity_status = "mismatch"
        else:
            identity_status = "matched"
    elif not observed_title_keys:
        identity_status = "request_bound"
    elif len(observed_title_keys) > 1:
        reasons.append("reason:multiple_primary_titles")
        identity_status = "mismatch"
    else:
        identity_status = "matched"

    if reasons:
        return "mismatch", tuple(evidence), tuple(reasons)
    if identity_status == "unverified":
        return "unverified", tuple(evidence), ("reason:title_observation_missing",)
    return identity_status, tuple(evidence), ()
