from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from .models import (
    AnalysisStatus,
    Direction,
    DIRECTION_LABELS,
    DirectionClassification,
    ScreeningEvidence,
    ScreeningResult,
)
from packages.recruitment_core.jd_capture import assess_jd_capture


DEFAULT_DIRECTIONS = tuple(Direction)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _nested_field(value: Any, name: str, default: Any = None) -> Any:
    direct = _field(value, name, None)
    if direct is not None:
        return direct
    matching = _field(value, "matching", None)
    return _field(matching, name, default) if matching is not None else default


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _compact(value: Any) -> str:
    return " ".join(_text(value).split())


def _excerpt(value: str, limit: int = 180) -> str:
    return _compact(value)[:limit]


def _job_text(job: Any, *names: str) -> str:
    for name in names:
        value = _text(_field(job, name, None))
        if value:
            return value
    return ""


def job_id(job: Any) -> str:
    return _compact(_field(job, "id", None) or _field(job, "source_ref", ""))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_fingerprint(value: Any) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def job_content_payload(job: Any) -> dict[str, Any]:
    """Build a source-independent content payload for incremental matching."""
    return {
        "company": _compact(
            _field(job, "company", None) or _field(job, "company_id", "")
        ),
        "title": _compact(_field(job, "title", "")),
        "city": _compact(_field(job, "city", "")),
        "job_type": _compact(_field(job, "job_type", "")),
        "batch": _compact(_field(job, "batch", "")),
        "cohort": _field(job, "cohort", None),
        "cohort_status": _compact(_field(job, "cohort_status", "")),
        "jd_raw": _compact(_field(job, "jd_raw", "")),
    }


def content_fingerprint(job: Any) -> str:
    return stable_fingerprint(job_content_payload(job))


def jd_fingerprint(job: Any) -> str:
    """Compatibility alias used by the legacy persistence vocabulary."""
    return content_fingerprint(job)


def profile_content_payload(profile: Any) -> dict[str, Any]:
    fields = (
        "degree",
        "job_type",
        "direction",
        "direction_policy",
        "primary_directions",
        "secondary_directions",
        "target_directions",
        "skills",
        "project_evidence",
        "supporting_skills",
        "learning_targets",
        "unverified_skills",
        "excluded_unverified_skills",
        "evidence",
    )
    payload: dict[str, Any] = {}
    for name in fields:
        value = _nested_field(profile, name, None)
        if value is not None:
            payload[name] = _json_value(value)
    return payload


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def profile_fingerprint(profile: Any) -> str:
    return stable_fingerprint(profile_content_payload(profile))


_INTERN_RE = re.compile(r"(^|[^a-z])intern(ship)?([^a-z]|$)", re.I)
_INTERN_PROJECT_RE = re.compile(
    r"日常实习|应届实习|暑期实习|寒假实习|春季实习|秋季实习|长期实习|短期实习|"
    r"实习生(?:专项)?招聘(?:计划)?|实习招聘|可转正实习|实习薪酬(?:津贴)?|转正机会",
    re.I,
)
_INTERN_METADATA_RE = re.compile(r"(?:^|[\s|·])实习(?:[\s|·]|$)", re.I)
_INTERN_BODY_EN_RE = re.compile(
    r"(?:complete|undertake|join)\s+an?\s+internship|"
    r"internship\s+(?:position|program|programme|opportunity|duration)",
    re.I,
)
_INTERN_DURATION_RE = re.compile(
    r"(?:至少|能够|能|可)?实习(?:期)?(?:至少|不少于)?\s*"
    r"[一二三四五六七八九十两\d]+\s*个?月|"
    r"每周(?:至少)?(?:到岗|实习)\s*[一二三四五六七八九十两\d]+\s*天",
    re.I,
)
_INTERN_COMMITMENT_RE = re.compile(
    r"(?:^|[。；;，,！!？?\n])\s*(?:\d+[.、）)]\s*)?"
    r"(?:(?:能(?:够)?|可(?:以)?|需(?:要)?|须|必须)\s*)?"
    r"(?:保证|保障|确保)\s*(?:持续|稳定)(?:的)?\s*"
    r"实习(?:投入)?(?:时间|时长|出勤|到岗)",
    re.I,
)
_INTERN_URL_RE = re.compile(
    r"(?:[?&#](?:type|recruitType)=|/)(?:internship|intern)(?:[/?&#]|$)",
    re.I,
)
_DOCTORATE_ONLY_RE = re.compile(
    r"仅限博士|只招博士|要求博士|博士(?:学历|学位|毕业|研究生)|博士及以上|"
    r"(?:学历|学位|毕业要求)\s*[:：]?\s*(?:博士|Ph\.?D\.?)",
    re.I,
)
_DOCTORATE_TITLE_RE = re.compile(r"博士|\bPh\.?D\.?\b", re.I)
_NON_DOCTORATE_ALTERNATIVE_RE = re.compile(
    r"本科(?:及以上|或硕士|/硕士|、硕士)|"
    r"硕士(?:及以上|或博士|/博士|、博士)|硕博|研究生及以上|博士优先",
    re.I,
)
_EARLY_BATCH_RE = re.compile(r"提前批|提前招聘|提前选拔|预招聘|early\s*batch", re.I)


def is_confirmed_2027(job: Any) -> bool:
    try:
        cohort = int(_field(job, "cohort", 0))
    except (TypeError, ValueError):
        cohort = 0
    return cohort == 2027 and _text(_field(job, "cohort_status", "")).casefold() == "confirmed"


def internship_reason(job: Any) -> str | None:
    title = _text(_field(job, "title", ""))
    job_type = _text(_field(job, "job_type", ""))
    batch = _text(_field(job, "batch", ""))
    body = _text(_field(job, "jd_raw", ""))
    url = _job_text(job, "jd_url", "detail_url")
    if "internship" in batch.casefold() or "实习" in batch:
        return "招聘批次明确标注实习"
    if "实习" in title or _INTERN_RE.search(title):
        return "标题明确标注实习"
    if "实习" in job_type or _INTERN_RE.search(job_type):
        return "招聘类型明确标注实习"
    if _INTERN_URL_RE.search(url):
        return "岗位 URL 明确属于实习轨道"
    if _INTERN_PROJECT_RE.search(body):
        return "招聘项目或正文明确标注实习"
    if "实习生" in body[:260] or _INTERN_METADATA_RE.search(body[:260]):
        return "页面头部元数据明确标注实习"
    if _INTERN_BODY_EN_RE.search(body):
        return "英文正文明确要求参加实习"
    if _INTERN_DURATION_RE.search(body) or _INTERN_COMMITMENT_RE.search(body):
        return "岗位要求当前候选人连续实习"
    return None


def is_internship(job: Any) -> bool:
    return internship_reason(job) is not None


def is_intern_job(job: Any) -> bool:
    """Compatibility name for the legacy filter vocabulary."""
    return is_internship(job)


def doctorate_reason(job: Any) -> str | None:
    title = _text(_field(job, "title", ""))
    body = " ".join(
        value
        for value in (
            _text(_field(job, "jd_raw", "")),
            _text(_field(job, "degree_requirement", "")),
            _text(_field(job, "education", "")),
        )
        if value
    )
    if _DOCTORATE_TITLE_RE.search(title):
        return "标题明确限定博士"
    if _NON_DOCTORATE_ALTERNATIVE_RE.search(body):
        return None
    if _DOCTORATE_ONLY_RE.search(body):
        return "正文明确限定博士"
    return None


def is_doctorate_only(job: Any) -> bool:
    return doctorate_reason(job) is not None


def is_doctorate_only_job(job: Any) -> bool:
    """Compatibility name for the legacy deterministic pre-screen."""
    return is_doctorate_only(job)


def is_jd_incomplete(job: Any) -> bool:
    return assess_jd_capture(job).incomplete


def _direction_key(value: Any) -> str:
    return re.sub(r"[\s_/\-（）()、，,：:]+", "", _text(value).casefold())


_DIRECTION_SPLIT_RE = re.compile(r"\s*(?:/|／|\||｜|、|,|，|;|；|\r?\n)\s*")


def _direction_parts(value: Any) -> tuple[str, ...]:
    text = _text(value)
    if not text:
        return ()
    return tuple(part for part in _DIRECTION_SPLIT_RE.split(text) if part.strip())


_DIRECTION_ALIASES: dict[str, Direction] = {
    _direction_key("C++"): Direction.CPP_SOFTWARE,
    _direction_key("C++软件开发"): Direction.CPP_SOFTWARE,
    _direction_key("C++与软件开发"): Direction.CPP_SOFTWARE,
    _direction_key("Linux系统软件"): Direction.CPP_SOFTWARE,
    _direction_key("Qt客户端"): Direction.CPP_SOFTWARE,
    _direction_key("软件开发"): Direction.CPP_SOFTWARE,
    _direction_key("测试开发"): Direction.CPP_SOFTWARE,
    _direction_key("测开"): Direction.CPP_SOFTWARE,
    _direction_key("ROS机器人软件"): Direction.ROBOT_ARM,
    _direction_key("机器人软件开发"): Direction.ROBOT_ARM,
    _direction_key("机器人与具身智能"): Direction.ROBOT_ARM,
    _direction_key("机械臂开发"): Direction.ROBOT_ARM,
    _direction_key("机器人开发"): Direction.ROBOT_ARM,
    _direction_key("具身智能"): Direction.EMBODIED_LEARNING,
    _direction_key("VLA"): Direction.EMBODIED_LEARNING,
    _direction_key("模仿学习"): Direction.EMBODIED_LEARNING,
    _direction_key("强化学习"): Direction.EMBODIED_LEARNING,
    _direction_key("大模型与智能体"): Direction.LLM_AGENT,
    _direction_key("大模型"): Direction.LLM_AGENT,
    _direction_key("Agent开发"): Direction.LLM_AGENT,
    _direction_key("RAG"): Direction.LLM_AGENT,
    _direction_key("AI应用"): Direction.LLM_AGENT,
    _direction_key("模型训练部署"): Direction.LLM_AGENT,
}
_DIRECTION_ALIASES.update(
    {
        _direction_key(Direction.CPP_SOFTWARE.value): Direction.CPP_SOFTWARE,
        _direction_key(Direction.ROBOT_ARM.value): Direction.ROBOT_ARM,
        _direction_key(Direction.EMBODIED_LEARNING.value): Direction.EMBODIED_LEARNING,
        _direction_key(Direction.LLM_AGENT.value): Direction.LLM_AGENT,
    }
)


def canonical_direction(value: Any) -> Direction | None:
    key = _direction_key(value)
    if not key:
        return None
    if key in _DIRECTION_ALIASES:
        return _DIRECTION_ALIASES[key]
    if any(token in key for token in ("具身", "vla", "embodied", "模仿学习", "强化学习")):
        return Direction.EMBODIED_LEARNING
    if any(
        token in key
        for token in ("大模型", "llm", "agent", "智能体", "rag", "ai应用", "模型训练")
    ):
        return Direction.LLM_AGENT
    if any(
        token in key
        for token in ("c++", "cpp", "qt", "linux", "ubuntu", "测开", "测试开发")
    ):
        return Direction.CPP_SOFTWARE
    if "机械臂" in key or "机器人" in key or "robot" in key:
        return Direction.ROBOT_ARM
    if any(token in key for token in ("软件", "客户端", "系统工程")):
        return Direction.CPP_SOFTWARE
    return None


def requested_directions(profile: Any) -> list[Direction]:
    raw_values: list[Any] = []
    target = _nested_field(profile, "target_directions", None)
    if isinstance(target, Sequence) and not isinstance(target, (str, bytes)):
        raw_values.extend(target)
    elif target:
        raw_values.append(target)
    for name in ("primary_directions", "secondary_directions"):
        values = _nested_field(profile, name, None)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            raw_values.extend(values)
        elif values:
            raw_values.append(values)
    direction = _nested_field(profile, "direction", None)
    if direction:
        raw_values.append(direction)
    resolved: list[Direction] = []
    for value in raw_values:
        for part in _direction_parts(value):
            item = canonical_direction(part)
            if item is not None and item not in resolved:
                resolved.append(item)
    return resolved or list(DEFAULT_DIRECTIONS)


_CPP_DIRECT = (
    ("C++", re.compile(r"C\s*(?:\+\+|/\s*C\+\+)|\bCPP\b|C语言|(?<![A-Za-z])Qt(?![A-Za-z])", re.I)),
    ("测试开发", re.compile(r"测试开发|测开|SDET|自动化测试|软件测试|研发测试", re.I)),
)
_CPP_SOFTWARE_TITLE = re.compile(
    r"软件(?:开发|研发|工程师)|系统软件|平台软件|应用软件|客户端|服务端|后端|"
    r"开发工程师|研发工程师|系统工程师|软件工程",
    re.I,
)
_CPP_STACK = re.compile(
    r"C\s*(?:\+\+|/\s*C\+\+)|\bCPP\b|C语言|(?<![A-Za-z])Qt(?![A-Za-z])|Linux|Ubuntu|Windows|"
    r"多线程|操作系统|客户端|桌面端",
    re.I,
)
_NON_CPP_TITLE = re.compile(r"Java|前端|Web|Android|iOS|Golang|Go语言|PHP|数据库|大数据", re.I)
_NON_TECHNICAL_TITLE_RE = re.compile(
    r"产品(?:经理|总经理|运营|规划|专员)|中台产品|Product\s+Manager|"
    r"运营|销售|市场|商务|渠道|客户成功|客服|品牌|营销|公关|"
    r"行政|财务|会计|审计|税务|人力资源|HRBP|采购|供应链|物流|法务|"
    r"管培生|培训生|策划|内容|社区|投融资",
    re.I,
)
_TECHNICAL_ADJACENT_TITLE_RE = re.compile(
    r"技术产品|产品技术|解决方案架构师|技术架构师|技术顾问|技术支持工程师",
    re.I,
)
_PRODUCT_MANAGER_TITLE_RE = re.compile(r"产品(?:经理|总经理|运营|规划|专员)|Product\s+Manager", re.I)
_EXPLICIT_TECHNICAL_ROLE_TITLE_RE = re.compile(
    r"算法|开发|研发|研究|科研|软件|嵌入式|编译器|中间件|测试|测开",
    re.I,
)
_TECHNICAL_TITLE_RE = re.compile(
    r"算法|工程师|开发|研发|研究|科研|科学家|软件|测试|测开|机器人|机械臂|"
    r"具身|视觉|控制|仿真|嵌入式|硬件|芯片|模型|系统|平台|架构|编译器|中间件|"
    r"技术",
    re.I,
)
_TECHNICAL_BODY_RE = re.compile(
    r"C\s*(?:\+\+|/\s*C\+\+)|\bCPP\b|软件(?:开发|研发)|算法(?:开发|研发)?|"
    r"模型(?:训练|部署|推理|微调)|机器人(?:软件|算法)?|技术研发|系统开发|"
    r"代码|编程|工程实现|自动化测试",
    re.I,
)
_TECHNICAL_STACK_RE = (
    re.compile(r"C\s*(?:\+\+|/\s*C\+\+)|\bCPP\b|Python|Java|Go|Rust", re.I),
    re.compile(r"Linux|Ubuntu|ROS\s*2?|Git|Docker|Kubernetes", re.I),
    re.compile(r"PyTorch|TensorFlow|CUDA|API|SDK|数据库|多线程|网络编程", re.I),
)
_BONUS_SECTION_RE = re.compile(
    r"(?:加分项|加分条件|加分要求|优先条件|preferred\s+qualifications?)\s*[:：]?",
    re.I,
)
_NEXT_SECTION_RE = re.compile(
    r"职位描述|岗位描述|职位职责|岗位职责|工作职责|工作内容|职位介绍|"
    r"任职要求|岗位要求|职位要求|任职资格|招聘要求|学历要求|responsibilities|"
    r"requirements|qualifications",
    re.I,
)
_ROBOT_SIGNALS = (
    ("机械臂/机器人", re.compile(r"机械臂|机械手|机器人|robot\s*arm|robotics", re.I)),
    ("控制/规划", re.compile(r"运动控制|运动规划|轨迹规划|路径规划|抓取|SLAM|导航", re.I)),
    ("ROS", re.compile(r"(?<![A-Za-z])ROS(?:\s*2)?(?![A-Za-z])", re.I)),
)
_EMBODIED_SIGNALS = (
    ("具身/VLA", re.compile(r"具身|具身智能|(?<![A-Za-z])(?:VLA|VLM|embodied)(?![A-Za-z])", re.I)),
    ("模仿学习", re.compile(r"模仿学习|行为克隆|imitation\s+learning", re.I)),
    ("强化学习", re.compile(r"强化学习|深度强化学习|reinforcement\s+learning", re.I)),
)
_LLM_SIGNALS = (
    ("大模型/LLM", re.compile(r"大模型|语言模型|(?<![A-Za-z])(?:LLM|AIGC)(?![A-Za-z])|生成式模型|多模态", re.I)),
    ("Agent/RAG", re.compile(r"智能体|(?<![A-Za-z])(?:agent(?:ic|s)?|RAG(?:Flow)?)(?![A-Za-z])|function\s+calling|tool\s+use", re.I)),
    ("训练/部署/推理", re.compile(r"模型训练|训练部署|模型部署|推理服务|模型推理|微调", re.I)),
    ("AI应用", re.compile(r"AI应用|大模型应用|人工智能应用|AI\s*应用", re.I)),
)
_LINUX_SIGNAL = ("Linux", re.compile(r"Linux|Ubuntu", re.I))
_ROS_SIGNAL = ("ROS", re.compile(r"(?<![A-Za-z])ROS(?:\s*2)?(?![A-Za-z])", re.I))


def _remove_bonus_sections(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _BONUS_SECTION_RE.finditer(text):
        pieces.append(text[cursor : match.start()])
        next_match = _NEXT_SECTION_RE.search(text, match.end())
        cursor = next_match.start() if next_match else len(text)
    pieces.append(text[cursor:])
    return " ".join(pieces)


def _is_non_technical_title(title: str) -> bool:
    if not _NON_TECHNICAL_TITLE_RE.search(title):
        return False
    if _TECHNICAL_ADJACENT_TITLE_RE.search(title):
        return False
    if _PRODUCT_MANAGER_TITLE_RE.search(title):
        return True
    return not _EXPLICIT_TECHNICAL_ROLE_TITLE_RE.search(title)


def _has_technical_role(title: str, body: str) -> bool:
    if _is_non_technical_title(title):
        return False
    if _TECHNICAL_TITLE_RE.search(title):
        return True
    core_body = _remove_bonus_sections(body)
    body_signal = _TECHNICAL_BODY_RE.search(core_body) is not None
    stack_hits = sum(pattern.search(core_body) is not None for pattern in _TECHNICAL_STACK_RE)
    return body_signal and stack_hits >= 1


def _first_signal(
    sources: Sequence[tuple[str, str]],
    patterns: Sequence[tuple[str, re.Pattern[str]]],
) -> tuple[str, str, str] | None:
    for source, text in sources:
        for label, pattern in patterns:
            match = pattern.search(text)
            if match:
                return source, label, match.group(0)
    return None


def _evidence(
    *,
    source: str,
    signal: str,
    excerpt: str,
    direction: Direction | None = None,
    reason: str = "",
) -> ScreeningEvidence:
    return ScreeningEvidence(
        source=source,
        signal=signal,
        excerpt=_excerpt(excerpt),
        direction=direction,
        reason=reason,
    )


def classify_job_directions(job: Any) -> DirectionClassification:
    title = _text(_field(job, "title", ""))
    body = _text(_field(job, "jd_raw", ""))
    job_type = _text(_field(job, "job_type", ""))
    core_body = _remove_bonus_sections(body)
    sources = ("title", title), ("jd_raw", core_body), ("job_type", job_type)
    evidence: list[ScreeningEvidence] = []
    matched: list[Direction] = []
    technical_role = _has_technical_role(title, body)

    direct_cpp = _first_signal(sources, _CPP_DIRECT) if technical_role else None
    software_title = _CPP_SOFTWARE_TITLE.search(title) is not None
    non_cpp_title = _NON_CPP_TITLE.search(title) is not None
    stack = _CPP_STACK.search(f"{title}\n{core_body}") is not None
    if technical_role and (direct_cpp or (software_title and stack and not non_cpp_title)):
        source, signal, excerpt = direct_cpp or ("title", "软件开发+工程栈", title)
        matched.append(Direction.CPP_SOFTWARE)
        evidence.append(
            _evidence(
                source=source,
                signal=signal,
                excerpt=excerpt,
                direction=Direction.CPP_SOFTWARE,
                reason=DIRECTION_LABELS[Direction.CPP_SOFTWARE],
            )
        )

    robot_signal = _first_signal(sources, _ROBOT_SIGNALS[:2]) if technical_role else None
    ros_signal = _first_signal(sources, (_ROBOT_SIGNALS[2],))
    if robot_signal:
        source, signal, excerpt = robot_signal
        matched.append(Direction.ROBOT_ARM)
        evidence.append(
            _evidence(
                source=source,
                signal=signal,
                excerpt=excerpt,
                direction=Direction.ROBOT_ARM,
                reason=DIRECTION_LABELS[Direction.ROBOT_ARM],
            )
        )
    elif ros_signal and technical_role and re.search(
        r"机器人|机械臂|控制|规划|导航|开发|软件|算法|工程师|研究|研发",
        title,
        re.I,
    ):
        source, signal, excerpt = ros_signal
        matched.append(Direction.ROBOT_ARM)
        evidence.append(
            _evidence(
                source=source,
                signal=signal,
                excerpt=excerpt,
                direction=Direction.ROBOT_ARM,
                reason="ROS 作为机器人开发证据",
            )
        )

    embodied_signal = _first_signal(sources, _EMBODIED_SIGNALS) if technical_role else None
    if embodied_signal:
        source, signal, excerpt = embodied_signal
        matched.append(Direction.EMBODIED_LEARNING)
        evidence.append(
            _evidence(
                source=source,
                signal=signal,
                excerpt=excerpt,
                direction=Direction.EMBODIED_LEARNING,
                reason=DIRECTION_LABELS[Direction.EMBODIED_LEARNING],
            )
        )

    llm_signal = _first_signal(sources, _LLM_SIGNALS) if technical_role else None
    if llm_signal:
        source, signal, excerpt = llm_signal
        matched.append(Direction.LLM_AGENT)
        evidence.append(
            _evidence(
                source=source,
                signal=signal,
                excerpt=excerpt,
                direction=Direction.LLM_AGENT,
                reason=DIRECTION_LABELS[Direction.LLM_AGENT],
            )
        )

    supporting: list[ScreeningEvidence] = []
    for patterns in (_LINUX_SIGNAL, _ROS_SIGNAL):
        signal = _first_signal(sources, (patterns,))
        if signal:
            source, label, excerpt = signal
            supporting.append(
                _evidence(
                    source=source,
                    signal=label,
                    excerpt=excerpt,
                    reason="辅助工程栈证据，不单独构成目标方向",
                )
            )

    ordered = [direction for direction in DEFAULT_DIRECTIONS if direction in matched]
    return DirectionClassification(
        matched_directions=ordered,
        primary_match_direction=ordered[0] if ordered else None,
        evidence=evidence,
        supporting_evidence=supporting,
    )


def classify_directions(job: Any) -> DirectionClassification:
    return classify_job_directions(job)


def screen_job(job: Any, profile: Any | None = None) -> ScreeningResult:
    classification = classify_job_directions(job)
    evidence = [*classification.evidence, *classification.supporting_evidence]
    if not is_confirmed_2027(job):
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.COHORT_UNCONFIRMED,
            reasons=[AnalysisStatus.COHORT_UNCONFIRMED.value],
            evidence=[
                _evidence(
                    source="cohort",
                    signal="cohort_gate",
                    excerpt=(
                        f"cohort={_field(job, 'cohort', None)}, "
                        f"cohort_status={_field(job, 'cohort_status', None)}"
                    ),
                    reason="仅允许确认的 2027 届岗位",
                ),
                *evidence,
            ],
            matched_directions=classification.matched_directions,
            primary_match_direction=classification.primary_match_direction,
            supporting_evidence=classification.supporting_evidence,
        )
    raw_batch = _text(_field(job, "batch", "")).strip().lower()
    batch_fields = (
        raw_batch,
        _field(job, "recruitment_track", ""),
        _field(job, "title", ""),
        _field(job, "job_type", ""),
        _field(job, "campaign_text", ""),
        _field(job, "jd_raw", ""),
    )
    batch = " ".join(_text(value) for value in batch_fields if _text(value))
    if raw_batch in {"early", "early_batch", "提前批"} or _EARLY_BATCH_RE.search(batch):
        evidence.insert(
            0,
            _evidence(
                source="batch",
                signal="early_batch_allowed",
                excerpt=batch,
                reason="已确认的 2027 提前批按计划进入匹配评分",
            ),
        )
    internship = internship_reason(job)
    if internship:
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.INTERNSHIP,
            reasons=[AnalysisStatus.INTERNSHIP.value],
            evidence=[
                _evidence(
                    source="internship",
                    signal="internship_gate",
                    excerpt=internship,
                    reason=internship,
                ),
                *evidence,
            ],
            matched_directions=classification.matched_directions,
            primary_match_direction=classification.primary_match_direction,
            supporting_evidence=classification.supporting_evidence,
        )
    doctorate = doctorate_reason(job)
    if doctorate:
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.DOCTORATE_ONLY,
            reasons=[AnalysisStatus.DOCTORATE_ONLY.value],
            evidence=[
                _evidence(
                    source="degree",
                    signal="doctorate_gate",
                    excerpt=doctorate,
                    reason=doctorate,
                ),
                *evidence,
            ],
            matched_directions=classification.matched_directions,
            primary_match_direction=classification.primary_match_direction,
            supporting_evidence=classification.supporting_evidence,
        )
    requested = (
        requested_directions(profile)
        if profile is not None
        else list(DEFAULT_DIRECTIONS)
    )
    matched = [
        direction
        for direction in requested
        if direction in classification.matched_directions
    ]
    # A sparse, unclassified title needs details before it can be ruled out.
    jd_quality = assess_jd_capture(job)
    if jd_quality.incomplete and (matched or not classification.matched_directions):
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.JD_INCOMPLETE,
            reasons=[AnalysisStatus.JD_INCOMPLETE.value],
            evidence=[
                _evidence(
                    source="jd_raw",
                    signal=f"jd_capture:{jd_quality.reason_code}",
                    excerpt=_field(job, "jd_raw", ""),
                    reason=jd_quality.reason,
                ),
                *evidence,
            ],
            matched_directions=classification.matched_directions,
            primary_match_direction=classification.primary_match_direction,
            supporting_evidence=classification.supporting_evidence,
        )

    if not matched:
        title = _text(_field(job, "title", ""))
        if _is_non_technical_title(title):
            direction_signal = "non_technical_role"
            direction_reason = "标题明确属于产品、运营、销售等非技术岗位，不因单个技术关键词放行"
        else:
            direction_signal = "direction_evidence_missing"
            direction_reason = "未发现目标方向所需的技术岗位证据组合"
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.DIRECTION_OUT,
            reasons=[AnalysisStatus.DIRECTION_OUT.value],
            evidence=[
                _evidence(
                    source="direction",
                    signal=direction_signal,
                    excerpt=title,
                    reason=direction_reason,
                ),
                *evidence,
            ],
            matched_directions=classification.matched_directions,
            primary_match_direction=classification.primary_match_direction,
            supporting_evidence=classification.supporting_evidence,
        )
    return ScreeningResult(
        eligible=True,
        analysis_status=AnalysisStatus.ELIGIBLE,
        reasons=[],
        evidence=evidence,
        matched_directions=matched,
        primary_match_direction=matched[0],
        supporting_evidence=classification.supporting_evidence,
    )


def is_analysis_eligible(job: Any, profile: Any | None = None) -> bool:
    return screen_job(job, profile).eligible


def filter_target_jobs(
    jobs: Sequence[Any], profile: Any | None = None
) -> tuple[list[Any], list[Any]]:
    kept: list[Any] = []
    dropped: list[Any] = []
    for job in jobs:
        (kept if is_analysis_eligible(job, profile) else dropped).append(job)
    return kept, dropped


__all__ = [
    "DEFAULT_DIRECTIONS",
    "canonical_direction",
    "classify_directions",
    "classify_job_directions",
    "content_fingerprint",
    "doctorate_reason",
    "filter_target_jobs",
    "internship_reason",
    "is_analysis_eligible",
    "is_confirmed_2027",
    "is_doctorate_only",
    "is_doctorate_only_job",
    "is_intern_job",
    "is_internship",
    "is_jd_incomplete",
    "jd_fingerprint",
    "job_content_payload",
    "job_id",
    "profile_content_payload",
    "profile_fingerprint",
    "requested_directions",
    "screen_job",
    "stable_fingerprint",
]
