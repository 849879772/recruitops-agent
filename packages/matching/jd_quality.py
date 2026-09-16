from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class JdQuality:
    """Deterministic, queue-friendly assessment of one stored JD."""

    complete: bool
    reason_code: str
    reason: str
    text_length: int = 0
    raw_length: int = 0

    @property
    def incomplete(self) -> bool:
        return not self.complete


_DUTY_HEADING_RE = re.compile(
    r"职位描述|岗位描述|职位职责|岗位职责|工作职责|工作内容|职位介绍|主要职责|"
    r"\bresponsibilities\b|\bjob\s+description\b",
    re.I,
)
_REQUIREMENT_HEADING_RE = re.compile(
    r"任职要求|岗位要求|职位要求|工作要求|任职资格|招聘要求|学历要求|基本资格|"
    r"\brequirements\b|\bqualifications\b",
    re.I,
)
_DUTY_LANGUAGE_RE = re.compile(
    r"负责|参与|设计|开发|研发|维护|优化|测试|部署|实现|构建|推进|协作|解决|支持|探索|"
    r"研究(?!生|所|院|员|团队|能力|经历|经验|背景|方向)|"
    r"\b(?:responsible\s+for|design(?:ing)?|develop(?:ing)?|build(?:ing)?|"
    r"implement(?:ing)?|maintain(?:ing)?|optimi[sz](?:e|ing)|test(?:ing)?|"
    r"deploy(?:ing)?|debug(?:ging)?|writ(?:e|ing)|investigat(?:e|ing)|"
    r"collaborat(?:e|ing)|support(?:ing)?|improv(?:e|ing)|research(?:ing)?)\b",
    re.I,
)
_REQUIREMENT_LANGUAGE_RE = re.compile(
    r"熟悉|掌握|具备|具有|拥有|能够|了解|精通|熟练(?:使用|掌握)?|"
    r"本科|硕士|博士|毕业|有(?=[^。；;\n]{1,35}(?:经验|经历|基础))|"
    r"(?:良好|扎实|较强)(?=[^。；;\n]{1,25}(?:能力|基础))|"
    r"\b(?:experience|familiarity(?:\s+with)?|familiar\s+with|"
    r"proficien(?:t\s+in|cy(?:\s+in)?)|knowledge(?:\s+of)?|"
    r"understanding(?:\s+of)?|ability\s+to|skilled\s+in|"
    r"bachelor(?:'s)?|master(?:'s)?|degree|ph\.?d\.?)\b",
    re.I,
)
_NUMBERED_ITEM_RE = re.compile(
    r"(?:^|[\n\r；;])\s*(?:[-*•·]|\(?\d{1,2}\)?[.、:：)）])\s*\S+",
    re.M,
)
_PUBLISHED_ONLY_RE = re.compile(r"发布于\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")
_BONUS_SECTION_RE = re.compile(
    r"(?:加分项|加分条件|加分要求|优先条件|期望资格|preferred\s+qualifications?)"
    r"\s*[:：]?",
    re.I,
)
_SECTION_HEADING_RE = re.compile(
    rf"(?P<bonus>{_BONUS_SECTION_RE.pattern})|"
    rf"(?P<duty>{_DUTY_HEADING_RE.pattern})|"
    rf"(?P<requirement>{_REQUIREMENT_HEADING_RE.pattern})",
    re.I,
)
_NAVIGATION_RE = re.compile(
    r"首页|返回|上一页|下一页|职位列表|招聘职位|全部职位|相关职位|相似职位|"
    r"更多职位|推荐职位|搜索|筛选|申请职位|投递职位|收藏|分享|登录|"
    r"职位类别|工作地点|所属部门|部门意向|招聘类型|校园招聘|全职",
    re.I,
)
_JOB_IDENTITY_RE = re.compile(
    r"(?:职位名称|岗位名称|职位标题)\s*[:：|]\s*([^\n\r|；;，,]{2,80})",
    re.I,
)
_JOB_LIST_RE = re.compile(
    r"(?<!\w)(?:职位列表|招聘职位|全部职位|相关职位|相似职位|更多职位|推荐职位|"
    r"相关岗位|相似岗位|更多岗位|推荐岗位)(?=$|[\s|:：])",
    re.I,
)
_BLOCK_FOOTER_RE = re.compile(
    r"申请职位|投递职位|职位列表|相关职位|相似职位|更多职位|推荐职位|"
    r"立即投递|(?:20\d{2}届(?:[-\s]*(?:秋招|春招|正式批|提前批))?\s*)?"
    r"允许(?:\d+\s*个?(?:月|天|年)内|一年内|半年内)?投递\s*\d+\s*(?:次|个职位)|"
    # Navigation words inside prose (e.g. 技术分享, 首页推荐) are not footers.
    r"(?<!\w)(?:工作地点|所属部门|部门意向)(?=\s|[:：]|$)|"
    r"(?<!\w)(?:收藏|分享|返回|首页)"
    r"(?=\s*(?:$|[\r\n|]|收藏|分享|返回|首页|申请职位|投递职位))",
    re.I,
)
_APPLICATION_FOOTER_LABEL_RE = re.compile(
    r"(?P<label>[\u3400-\u9fffA-Za-z0-9]{1,32}"
    r"(?:计划|项目|专项|招聘|校招|应届生)"
    r"[\u3400-\u9fffA-Za-z0-9]{0,16}[-—_：:|])$"
)
_TERMINAL_CHARS = frozenset("。！？!?；;.)]】》")
_DANGLING_END_RE = re.compile(r"(?:[，,、：:；;]|以及|并且|并|和|或|与|及)$")
_CLAUSE_SPLIT_RE = re.compile(r"[\r\n；;。!?！？]+|(?<=[a-z.])[.](?=\s|$)", re.I)
_ITEM_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|\(?\d{1,2}[.、:：)）])\s*")
_STRUCTURED_ACTIVITY_RE = re.compile(
    r"负责|参与|协助|制定|建立|输出|监控|分析|挖掘|生成|编写|推动|跟踪|响应|"
    r"规划|运用|探索|沉淀|组织|运营|上架|回复|处理|调研|选品|复盘|管控|"
    r"监测|检测|控制|开发|研发|设计|维护|优化|测试|部署|实现|构建|协作|"
    r"解决|支持|排查|适配|标定|整定|交付|撰写|梳理|收集|评审|发布|提效|管理|"
    r"\b(?:monitor(?:ing)?|analy[sz](?:e|ing)|operat(?:e|ing)|track(?:ing)?|"
    r"plan(?:ning)?|review(?:ing)?|process(?:ing)?|deliver(?:y|ing)?)\b",
    re.I,
)
_STRUCTURED_ROLE_SUFFIX_RE = re.compile(
    r"(?:工程师|专员|经理|主管|助理|岗位|职位|专业|方向|序列)$"
)
_STRUCTURED_GENERIC_ACTIVITY_RE = re.compile(
    r"相关|优先|以及|并且|工作|事项|任务|内容|职责|岗位|职位|要求|条件|"
    r"\b(?:and|or|with|the|to|for)\b",
    re.I,
)
_STRUCTURED_QUALIFICATION_LANGUAGE_RE = re.compile(
    r"CET\s*[-－]?\s*[46]|英语[四四六六]级|雅思|托福|TOEFL|IELTS|PMP|NPDP",
    re.I,
)
_STRUCTURED_CLAUSE_SPLIT_RE = re.compile(r"[\r\n；;。!?！？]+")
_STRUCTURED_MAJOR_RE = re.compile(
    r"(?P<subject>[A-Za-z\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff、，,()/（）·\s]{1,40}?)"
    r"(?:相关)?专业(?!能力|素养|技能|知识|水平|背景|要求)",
    re.I,
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _compact(value: Any) -> str:
    return " ".join(_text(value).split())


def _remove_bonus_sections(text: str) -> str:
    """Keep bonus-only mentions from satisfying the core JD signals."""

    pieces: list[str] = []
    cursor = 0
    for match in _BONUS_SECTION_RE.finditer(text):
        if match.start() < cursor:
            continue
        pieces.append(text[cursor : match.start()])
        next_match = _SECTION_HEADING_RE.search(text, match.end())
        end = next_match.start() if next_match else len(text)
        cursor = end
    pieces.append(text[cursor:])
    return "\n".join(pieces)


def _footer_start(text: str) -> int | None:
    match = _BLOCK_FOOTER_RE.search(text)
    if match is None:
        return None
    start = match.start()
    label = _APPLICATION_FOOTER_LABEL_RE.search(text[:start])
    if label is not None:
        start = label.start()
    return start


def _has_semantic_content(text: str, *, requirement: bool) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(text):
        clause = _ITEM_PREFIX_RE.sub("", clause).strip(" -|:：")
        duty = _DUTY_LANGUAGE_RE.search(clause)
        qualification = _REQUIREMENT_LANGUAGE_RE.search(clause)
        signal, other = (qualification, duty) if requirement else (duty, qualification)
        if signal is None or (other is not None and other.start() < signal.start()):
            continue
        # An action inside a candidate's experience is not a duty, and a product's
        # capabilities inside a duty are not candidate qualifications.
        remaining = _DUTY_LANGUAGE_RE.sub("", clause)
        remaining = _REQUIREMENT_LANGUAGE_RE.sub("", remaining)
        remaining = re.sub(
            r"相关|经验|经历|能力|技能|基础|优先|以及|并且|\b(?:in|of|with|and|to|for|the|a|an)\b",
            "",
            remaining,
            flags=re.I,
        )
        if re.search(r"[a-z\u3400-\u9fff]", remaining, re.I):
            return True
    return False


def _structured_clause_items(text: str) -> list[str]:
    """Split explicit section content into independently checkable items."""

    items: list[str] = []
    for clause in _STRUCTURED_CLAUSE_SPLIT_RE.split(text):
        clause = _ITEM_PREFIX_RE.sub("", clause).strip(" -|:：")
        if not clause:
            continue
        fragments = [
            fragment.strip(" -|:：")
            for fragment in re.split(r"[、，,/&]+", clause)
            if fragment.strip(" -|:：")
        ]
        activity_fragments = [
            fragment
            for fragment in fragments
            if _STRUCTURED_ACTIVITY_RE.search(fragment)
            or _DUTY_LANGUAGE_RE.search(fragment)
        ]
        if len(fragments) > 1 and len(activity_fragments) >= 2:
            items.extend(activity_fragments)
        else:
            items.append(clause)
    return items


def _structured_activity_signature(item: str) -> str:
    """Return a signature only for a work activity with a concrete object."""

    normalized = _ITEM_PREFIX_RE.sub("", item).strip(" -|:：")
    if not normalized or _STRUCTURED_ROLE_SUFFIX_RE.search(normalized):
        return ""
    if not (_STRUCTURED_ACTIVITY_RE.search(normalized) or _DUTY_LANGUAGE_RE.search(normalized)):
        return ""
    remaining = _DUTY_LANGUAGE_RE.sub("", normalized)
    remaining = _STRUCTURED_ACTIVITY_RE.sub("", remaining)
    remaining = _REQUIREMENT_LANGUAGE_RE.sub("", remaining)
    remaining = _STRUCTURED_GENERIC_ACTIVITY_RE.sub("", remaining)
    remaining = re.sub(r"[\s\d\W_]+", "", remaining, flags=re.UNICODE)
    if not re.search(r"[a-z\u3400-\u9fff]", remaining, re.I):
        return ""
    return re.sub(r"\s+", "", normalized).casefold()


def _structured_activity_count(text: str) -> int:
    signatures = {
        signature
        for item in _structured_clause_items(text)
        if (signature := _structured_activity_signature(item))
    }
    return len(signatures)


def _is_pure_technical_or_role_list(text: str) -> bool:
    """Reject only delimiter lists with no sentence-level work evidence."""

    if re.search(r"[。.!?！？；;]", text):
        return False
    fragments = [
        _ITEM_PREFIX_RE.sub("", fragment).strip(" -|:：")
        for fragment in re.split(r"[\r\n、，,/&]+", text)
        if fragment.strip(" -|:：")
    ]
    if len(fragments) < 2:
        return False
    if all(_STRUCTURED_ROLE_SUFFIX_RE.search(fragment) for fragment in fragments):
        return True
    return all(
        not (_STRUCTURED_ACTIVITY_RE.search(fragment) or _DUTY_LANGUAGE_RE.search(fragment))
        for fragment in fragments
    )


def _structured_requirement_dimensions(text: str) -> frozenset[str]:
    """Recognize distinct qualification dimensions without using text length."""

    dimensions: set[str] = set()
    for clause in _STRUCTURED_CLAUSE_SPLIT_RE.split(text):
        clause = _ITEM_PREFIX_RE.sub("", clause).strip(" -|:：")
        if not clause:
            continue
        if re.search(r"大专|中专|本科|硕士|博士|研究生", clause):
            dimensions.add("education")
        major = _STRUCTURED_MAJOR_RE.search(clause)
        if major and not re.search(r"(?:相关)?专业\s*(?:不限|无要求)", clause):
            dimensions.add("major")
        if _STRUCTURED_QUALIFICATION_LANGUAGE_RE.search(clause):
            dimensions.add("language_or_certificate")
        if re.search(r"熟悉|掌握|具备|具有|拥有|能够|了解|精通|熟练|经验|经历", clause):
            dimensions.add("skill_or_experience")
    return frozenset(dimensions)


def _section_has_structured_duty_evidence(content: str) -> bool:
    if _is_pure_technical_or_role_list(content):
        return False
    if _has_semantic_content(content, requirement=False):
        return True
    # A qualification list in the duty section must not be reinterpreted as
    # work evidence. Section inversion is deliberately handled elsewhere.
    if len(_structured_requirement_dimensions(content)) >= 2:
        return False
    return _structured_activity_count(content) >= 3


def _section_has_structured_requirement_evidence(content: str) -> bool:
    if _has_semantic_content(content, requirement=True):
        return True
    # A duty list in the requirement section must not be promoted to a
    # qualification list merely because it contains several action clauses.
    if _structured_activity_count(content) >= 3:
        return False
    return len(_structured_requirement_dimensions(content)) >= 2


def _jd_sections(text: str) -> dict[str, list[str]]:
    """Keep each heading's evidence within its own content boundary."""
    sections: dict[str, list[str]] = {"duty": [], "requirement": []}
    headings = list(_SECTION_HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        kind = heading.lastgroup
        if kind not in sections:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        content = text[heading.end() : end]
        footer_start = _footer_start(content)
        identity = _JOB_IDENTITY_RE.search(content)
        stops = [
            start
            for start in (footer_start, identity.start() if identity else None)
            if start is not None
        ]
        if stops:
            content = content[: min(stops)]
        sections[kind].append(content.strip(" -|:：\r\n"))
    return sections


def _has_structured_untitled_jd(text: str, remainder: str) -> bool:
    if len(remainder) < 220:
        return False
    clauses = [
        item.strip()
        for item in _CLAUSE_SPLIT_RE.split(text)
        if item.strip()
    ]
    semantic_clauses = [
        item
        for item in clauses
        if _has_semantic_content(item, requirement=False)
        or _has_semantic_content(item, requirement=True)
    ]
    has_multiple_items = (
        len(_NUMBERED_ITEM_RE.findall(text)) >= 3
        or len(semantic_clauses) >= 4
    )
    return bool(
        has_multiple_items
        and _has_semantic_content(text, requirement=False)
        and _has_semantic_content(text, requirement=True)
    )


def _normalize_jd_block(block: str) -> str:
    footer_start = _footer_start(block)
    if footer_start is not None:
        block = block[:footer_start]
    block = _JOB_IDENTITY_RE.sub("", block)
    block = _DUTY_HEADING_RE.sub("职责", block)
    block = _REQUIREMENT_HEADING_RE.sub("要求", block)
    block = re.sub(r"^【(职责|要求)】", r"\1", block)
    block = re.sub(r"^(职责|要求)[】\]]", r"\1", block)
    return re.sub(r"\s+", "", block).strip(" -|:：;；。[]【】")


def _jd_blocks(text: str) -> tuple[str, ...]:
    starts = list(_DUTY_HEADING_RE.finditer(text))
    blocks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = _normalize_jd_block(text[start.start() : end])
        if block:
            blocks.append(block)
    return tuple(blocks)


def _dedupe_jd_blocks(blocks: tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for block in blocks:
        if block not in unique:
            unique.append(block)
    return tuple(unique)


def _job_identity_values(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for match in _JOB_IDENTITY_RE.finditer(text):
        value = re.split(
            r"岗位职责|职位描述|职位职责|任职要求|岗位要求|申请职位|投递职位",
            match.group(1),
            maxsplit=1,
            flags=re.I,
        )[0]
        normalized = re.sub(r"\s+", "", value).strip(" -|:：;；。")
        if normalized and normalized not in values:
            values.append(normalized)
    return tuple(values)


def _has_cross_job_evidence(text: str) -> bool:
    identities = _job_identity_values(text)
    if len(identities) > 1:
        return True
    blocks = _dedupe_jd_blocks(_jd_blocks(text))
    if len(blocks) < 2:
        return False
    if _JOB_LIST_RE.search(text):
        return True
    # A nested duty heading or prose such as "不要求应届生有经验" is not
    # another complete job. Require both real sections in each distinct block.
    starts = list(_DUTY_HEADING_RE.finditer(text))
    complete_blocks = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start() : end]
        sections = _jd_sections(block)
        if all(any(_has_semantic_content(c, requirement=kind == "requirement")
                   for c in sections[kind]) for kind in ("duty", "requirement")):
            complete_blocks.append(_normalize_jd_block(block))
    return len(_dedupe_jd_blocks(tuple(complete_blocks))) > 1


def _quality(
    complete: bool,
    reason_code: str,
    reason: str,
    *,
    text_length: int,
    raw_length: int,
) -> JdQuality:
    return JdQuality(
        complete=complete,
        reason_code=reason_code,
        reason=reason,
        text_length=text_length,
        raw_length=raw_length,
    )


def assess_jd_quality(job_or_text: Any) -> JdQuality:
    """Assess JD content without treating a title or length alone as evidence."""

    raw_value = job_or_text if isinstance(job_or_text, str) else _field(job_or_text, "jd_raw", "")
    title = _compact(_field(job_or_text, "title", ""))
    raw_text = _text(raw_value)
    text = _compact(raw_text)
    text_length = len(text)
    raw_length = len(raw_text)
    if not text:
        return _quality(
            False,
            "empty_jd",
            "JD 正文为空，无法核对职责和任职要求",
            text_length=text_length,
            raw_length=raw_length,
        )

    remainder = text.replace(title, "", 1).strip(" -|:：") if title else text
    remainder = _PUBLISHED_ONLY_RE.sub("", remainder).strip(" -|:：")
    core_text = _remove_bonus_sections(raw_text)
    sections = _jd_sections(raw_text)
    duty_headings = sections["duty"]
    requirement_headings = sections["requirement"]
    duty_semantic = _has_semantic_content(core_text, requirement=False)
    requirement_semantic = _has_semantic_content(core_text, requirement=True)

    navigation_hits = {match.group(0).casefold() for match in _NAVIGATION_RE.finditer(text)}
    if not duty_headings and not requirement_headings and len(navigation_hits) >= 3:
        return _quality(
            False,
            "navigation_or_list_shell",
            "正文主要是招聘导航或职位列表元数据，不是单岗位 JD",
            text_length=text_length,
            raw_length=raw_length,
        )

    if _has_cross_job_evidence(raw_text):
        return _quality(
            False,
            "cross_job_content",
            "正文包含多个岗位区块或跨岗位导航，无法绑定到单一岗位",
            text_length=text_length,
            raw_length=raw_length,
        )

    if (
        not duty_semantic
        and not requirement_semantic
        and len(navigation_hits) >= 2
    ):
        return _quality(
            False,
            "navigation_or_list_shell",
            "正文缺少职责和要求语义，且主要是职位页面导航信息",
            text_length=text_length,
            raw_length=raw_length,
        )

    has_structured_content = _has_structured_untitled_jd(core_text, remainder)
    # Some detail APIs omit one heading while retaining its numbered clauses.
    # Require multiple independent clauses so an education/footer mention alone
    # cannot fill an otherwise missing section.
    implicit_requirements = has_structured_content and sum(
        _has_semantic_content(clause, requirement=True)
        for clause in _CLAUSE_SPLIT_RE.split(core_text)
    ) >= 2
    implicit_duties = has_structured_content and sum(
        _has_semantic_content(clause, requirement=False)
        for clause in _CLAUSE_SPLIT_RE.split(core_text)
    ) >= 2
    if duty_headings and not requirement_headings and not implicit_requirements:
        return _quality(
            False,
            "duties_without_requirements",
            "JD 只有岗位职责，缺少明确的任职要求",
            text_length=text_length,
            raw_length=raw_length,
        )
    if requirement_headings and not duty_headings and not implicit_duties:
        return _quality(
            False,
            "requirements_without_duties",
            "JD 只有任职要求，缺少明确的岗位职责",
            text_length=text_length,
            raw_length=raw_length,
        )

    has_explicit_sections = bool(duty_headings and requirement_headings)
    if not has_explicit_sections and not has_structured_content:
        if duty_semantic and not requirement_semantic:
            reason_code = "duties_without_requirements"
            reason = "JD 能识别到职责内容，但没有足够的任职要求证据"
        elif requirement_semantic and not duty_semantic:
            reason_code = "requirements_without_duties"
            reason = "JD 能识别到任职要求，但没有足够的职责内容证据"
        else:
            reason_code = "insufficient_detail"
            reason = "JD 缺少可核对的职责与任职要求结构"
        return _quality(
            False,
            reason_code,
            reason,
            text_length=text_length,
            raw_length=raw_length,
        )

    if has_explicit_sections:
        for kind, label in (("duty", "岗位职责"), ("requirement", "任职要求")):
            contents = sections[kind]
            has_evidence = (
                any(_section_has_structured_duty_evidence(content) for content in contents)
                if kind == "duty"
                else any(
                    _section_has_structured_requirement_evidence(content)
                    for content in contents
                )
            )
            if not has_evidence:
                empty = not any(re.search(r"[a-z\u3400-\u9fff]", c, re.I) for c in contents)
                return _quality(
                    False,
                    "empty_detail_section" if empty else f"{kind}_evidence_missing",
                    f"JD 的{label}区块缺少可核对的{'候选人资格' if kind == 'requirement' else '工作内容'}证据",
                    text_length=text_length,
                    raw_length=raw_length,
                )

    exact_limit = raw_length == 500 or text_length == 500
    requirement_tail = ""
    if requirement_headings:
        requirement_tail = _compact(requirement_headings[-1])
    likely_truncated = (
        exact_limit
        and (
            bool(_DANGLING_END_RE.search(text))
            or text[-1] not in _TERMINAL_CHARS and len(requirement_tail) < 80
        )
    )
    if likely_truncated:
        return _quality(
            False,
            "truncated_at_500",
            "JD 恰好触及 500 字截断边界，正文结尾或要求区块疑似被截断",
            text_length=text_length,
            raw_length=raw_length,
        )

    return _quality(
        True,
        "complete",
        "JD 同时包含可核对的岗位职责和任职要求",
        text_length=text_length,
        raw_length=raw_length,
    )


__all__ = ["JdQuality", "assess_jd_quality"]
