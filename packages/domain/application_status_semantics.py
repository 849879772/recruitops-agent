"""Reject status mentions that do not assert the proposed current stage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime


_CURRENT_LABEL = re.compile(
    r"(?:当前进度|申请进度|应聘进度|当前状态|最新状态|状态|\bstatus)\s*[:：]\s*([^\n]{1,120})", re.I,
)
_CURRENT_PREFIX = r"(?:当前进度|申请进度|应聘进度|当前状态|最新状态)\s*[:：]\s*"
_COMPOUND_WRITTEN_LABEL = r"笔试[ \t]*[/／][ \t]*AI语言测试"
_LITERAL_CURRENT_LABEL = re.compile(
    r"(?:" + _CURRENT_PREFIX + r"|(?<!\w)状态\s*[:：]\s*(?=简历等待筛选|用人部门筛选|简历复筛|初筛|"
    + _COMPOUND_WRITTEN_LABEL + r"))"
    r"(?P<label>简历等待筛选|用人部门筛选|简历复筛(?:\s*[-—–]\s*简历复筛)?|初筛|"
    + _COMPOUND_WRITTEN_LABEL + r"|"
    r"HR筛选\s*[-—–]\s*HR筛选\s*中|"
    r"HR筛选\s*[-—–·]\s*进行中|分配简历\s*[-—–:：]\s*流程中|"
    r"(?:HR|简历)?(?:筛选|初筛|评估|测评|笔试|面试|复试|终面)(?:\s*[·:：-]?\s*进行)?中|"
    r"(?:流程|申请流程)(?:已)?(?:结束|终止)|已撤回|已取消申请|已录用|已获得offer|offer已发放|"
    r"流程中|进行中)(?=$|\s|[，。；;])", re.I,
)


def literal_current_status_labels(text: str) -> list[str]:
    """Closed, verbatim current assertions, not a date or completed event badge."""
    labels = [match["label"] for match in _LITERAL_CURRENT_LABEL.finditer(text)]
    for match in re.finditer(r"(?<!\S)(?P<date>" + _SUBMISSION_DATE + r")\s+投递\s+"
                            r"(?:官网主投|官网投递|内推)\s+(?P<label>(?:简历)?(?:筛选|初筛|评估|"
                            r"测评|笔试|面试|复试|终面)(?:进行)?中)(?=$|\s|[，。；;])", text):
        if dated_submission_label(match["date"] + " 投递"):
            labels.append(match["label"])
    return list(dict.fromkeys(labels))


def literal_current_status_conflict(text: str) -> bool:
    labels = literal_current_status_labels(text)
    lines = text.splitlines()
    labels.extend(line.strip() for index, line in enumerate(lines) if not _dated_history_line(lines, index) and re.fullmatch(
        r"(?:HR|简历)?(?:筛选|初筛|评估|测评|笔试|面试|复试|终面)(?:\s*[·:：-]?\s*进行)?中|"
        r"(?:流程|申请流程)(?:已)?(?:结束|终止)", line.strip()))
    # Resume routing establishes no later stage, but cannot coexist as a second
    # explicit current assertion with a named test/interview node on this card.
    stages = {"applied" if is_resume_routing_status(label) else explicit_label_status(label)
              for label in labels} - {None}
    return len(stages) > 1


def _dated_history_line(lines: list[str], index: int) -> bool:
    """An adjacent, valid event date is history, not an independent current flag."""
    return (index + 1 < len(lines)
            and re.fullmatch(_SUBMISSION_DATE, lines[index + 1].strip()) is not None
            and dated_submission_label(lines[index + 1].strip() + " 投递") is not None)


_TIMELINE_STAGES = (
    r"申请成功|提交成功|已投递", r"筛选|初筛", r"笔试|机试", r"初试|一面",
    r"复试|二面", r"终面|终试", r"offer|录用",
)
_PROCESS_STEP_LABEL = re.compile(
    r"(?:申请成功|提交成功|已投递|投递成功|简历投递|投递简历|提交简历|投递|申请|网申|简历筛选|简历评估|"
    r"筛选|初筛|(?:在线|综合)?测评|(?:岗位|在线)?笔试|机试|(?:专业|综合|技术)?面试|"
    r"初试|复试|一面|二面|三面|终面|终试(?:洽谈)?|hr面|录用评估|录用|签约|入职|预入职|"
    r"offer|applied|screening|assessment|written(?: test)?|interview|hired)", re.I,
)


def _stage_count(text: str) -> int:
    return sum(bool(re.search(pattern, text, re.I)) for pattern in _TIMELINE_STAGES)


def status_evidence_conflict(card: Mapping) -> bool:
    """A parser's unselected process ladder is not mutually current evidence."""
    signals = card.get("signals") if isinstance(card.get("signals"), Mapping) else {}
    if not signals.get("conflicting_statuses"):
        return False
    if _current_label_beside_submission_date(card):
        return False
    labels = card.get("raw_status_labels")
    if isinstance(labels, list):
        labels = [label for label in labels if not dated_submission_label(str(label).strip())]
    explicit = _CURRENT_LABEL.search(str(card.get("context") or card.get("evidence") or ""))
    return not (signals.get("has_progress_timeline")
                and isinstance(labels, list) and bool(labels)
                and all(_PROCESS_STEP_LABEL.fullmatch(str(label).strip()) for label in labels)
                and not any(signals.get(key) for key in
                            ("has_active_step", "current_step_identified", "has_explicit_status"))
                and not (explicit and _stage_count(explicit.group(1)) <= 1))


_SUBMISSION_DATE = r"20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
_SUBMISSION_ACTION = r"(?:投递简历|简历投递|提交简历|已提交(?:简历)?|投递|提交|申请)"
_SUBMISSION_METADATA = r"(?:投递时间|申请时间|投递日期|申请日期|投递于|申请于)"
_DATED_SUBMISSION_LABEL = (
    _SUBMISSION_ACTION + r"\s+" + _SUBMISSION_DATE + r"|"
    + _SUBMISSION_DATE + r"[ \t]+" + _SUBMISSION_ACTION + r"|"
    + _SUBMISSION_METADATA + r"\s*[:：]?\s*" + _SUBMISSION_DATE
)


def dated_submission_label(label: str) -> str | None:
    """A complete dated submission label; card ownership is checked separately."""
    label = label.strip()
    if not re.fullmatch(_DATED_SUBMISSION_LABEL, label):
        return None
    date_text = re.search(_SUBMISSION_DATE, label)[0]
    parts = [int(value) for value in re.findall(r"\d+", date_text)]
    try:
        datetime(*(parts + [0] * (6 - len(parts))))
    except ValueError:
        return None
    return label


def _current_label_beside_submission_date(card: Mapping) -> str | None:
    """Recover only the legacy parser's one-current-label + date false conflict."""
    raw = card.get("raw_status_labels")
    if not isinstance(raw, list) or not any(dated_submission_label(str(label).strip()) for label in raw):
        return None
    signals = card.get("signals") if isinstance(card.get("signals"), Mapping) else {}
    if signals.get("has_progress_timeline") and not any(signals.get(key) for key in
            ("has_active_step", "current_step_identified", "has_explicit_status")):
        return None
    labels = [str(label or "").strip() for label in raw]
    labels.append(str(card.get("label") or "").strip())
    if signals.get("has_active_step") or signals.get("current_step_identified"):
        labels.append(str(card.get("current_step_label") or "").strip())
    current = list(dict.fromkeys(label for label in labels if label and not dated_submission_label(label)))
    # Different wording for the same canonical stage is not two incompatible
    # current assertions. Unknown labels and genuinely different stages fail closed.
    stages = {explicit_label_status(label) for label in current}
    if current and None not in stages and len(stages) == 1:
        return current[0]
    if len(current) == 1 and (noncanonical_status_reason(current[0]) or is_resume_routing_status(current[0])):
        # One explicit but unmapped current label plus its submission date is
        # unknown, not two incompatible current assertions. No stage is inferred.
        return current[0]
    return None


def _dated_submission_matches(text: str):
    for match in re.finditer(r"(?<!\S)(?:" + _DATED_SUBMISSION_LABEL + r")(?=$|\s|[，。；;])", text):
        if dated_submission_label(match[0]):
            yield match


def _dated_submission_labels(text: str) -> list[str]:
    return [match[0] for match in _dated_submission_matches(text)]


def dated_submission_baseline(card: Mapping) -> tuple[str, str] | None:
    """Only a dated action/receipt inside a uniquely bound personal record."""
    context = str(card.get("context") or card.get("evidence") or "")
    signals = card.get("signals") if isinstance(card.get("signals"), Mapping) else {}
    if (status_evidence_conflict(card) or not _dated_submission_labels(context)
            or signals.get("has_active_step") or signals.get("has_explicit_status")
            or signals.get("current_step_identified")
            or _current_label_beside_submission_date(card)):
        return None
    label = str(card.get("label") or "").strip()
    # The parser's asserted badge is current evidence even when its legacy
    # signal omitted has_explicit_status. A process ladder remains different.
    if (not signals.get("has_progress_timeline") and label and not dated_submission_label(label)
            and (explicit_label_status(label) is not None or is_resume_routing_status(label))):
        return None
    literal = literal_record_status(context)
    return literal if literal and literal[0] == "applied" else None


def timeline_without_current(card: Mapping) -> bool:
    """A whole process ladder is not evidence that any one step is current."""
    signals = card.get("signals") if isinstance(card.get("signals"), Mapping) else {}
    context = str(card.get("context") or card.get("evidence") or "")
    stages = card.get("stage_labels")
    stage_count = _stage_count(context)
    legacy_ladder = (stage_count >= 3 and (stage_count >= 4 or "流程" in context
                     or bool(re.search(r"(?:^|\s)1\s+.{0,300}?\s2\s+.{0,300}?\s3\s+|(?:→|->|➜|›)", context))))
    ladder = (signals.get("has_progress_timeline") or card.get("evidence_source") == "timeline-without-current"
              or (isinstance(stages, list) and len(stages) >= 3) or legacy_ladder)
    if not ladder:
        return False
    if dated_submission_baseline(card):
        return False
    identified = signals.get("has_active_step") or signals.get("current_step_identified")
    # A status-looking label in an old ladder capture may be the first step.
    # Only an active marker or a separate explicit current-state phrase resolves it.
    explicit = _CURRENT_LABEL.search(context)
    # “当前进度: 申请成功 -> 筛选 -> 笔试” still describes a whole ladder.
    explicit_current = explicit is not None and _stage_count(explicit.group(1)) <= 1
    return not (identified or explicit_current)


def current_status_labels(card: Mapping) -> list[str]:
    """Return quoteable current labels, never the collection of process steps."""
    baseline = dated_submission_baseline(card)
    if baseline:
        return [baseline[1]]
    if timeline_without_current(card):
        return []
    signals = card.get("signals") if isinstance(card.get("signals"), Mapping) else {}
    current = str(card.get("current_step_label") or "").strip()
    if current and (signals.get("current_step_identified") or signals.get("has_active_step")):
        return [current]
    current = _current_label_beside_submission_date(card)
    if current:
        return [current]
    label = str(card.get("label") or "").strip()
    literal = literal_record_status(str(card.get("context") or card.get("evidence") or ""))
    if label and not dated_submission_label(label) and (
        explicit_label_status(label) is not None or is_resume_routing_status(label)
    ):
        # Historical submission metadata does not replace a same-stage current
        # badge (for example HR筛选-进行中) with its old application date.
        return [label]
    if literal and (not label or explicit_label_status(label) in {None, literal[0]}):
        return [literal[1]]
    if label:
        return [label]
    labels = []
    raw = card.get("raw_status_labels")
    if isinstance(raw, list):
        labels.extend(str(label or "").strip() for label in raw)
    if not any(labels):
        match = _CURRENT_LABEL.search(str(card.get("context") or card.get("evidence") or ""))
        if match:
            labels.append(match.group(1).strip())
    labels = list(dict.fromkeys(label for label in labels if label))
    return labels if len(labels) == 1 else []


_STAGE_WORDS = {
    "applied": r"筛选|初筛|简历复筛|投递|申请|简历|测评|\b(?:screening|submitted|application|assessment)\b",
    "written": r"笔试|机试|编程测试|在线考试|\b(?:written\s*test|coding\s*test)\b",
    "interview": r"面试|初试|复试|一面|二面|三面|\binterviews?\b",
    "hr": r"终面|终试|洽谈|hr\s*面|人力面|\b(?:hr|final)\s*interviews?\b",
    "offer": r"offer|录用|入职|签约|\boffers?\b",
    "rejected": r"淘汰|不合适|未通过|不匹配|流程(?:已)?(?:终止|结束)|申请终止|拒绝|\b(?:rejected|unsuccessful)\b",
    "withdrawn": r"撤回|取消申请|\bwithdrawn\b",
}
_CLAUSE_GAP = r"[^，。；;,.!?！？\n]{0,200}"


def asserted_submission_label(label: str) -> str | None:
    """An explicit completed submission survives unrelated future boilerplate.

    Only the first asserted clause is accepted. Conditional/negated success and
    a separately asserted later outcome must still take the normal verifier.
    """
    first = re.split(r"[，。；;,.!?！？\n]", label.strip(), maxsplit=1)[0].strip()
    match = re.fullmatch(r"(?:您的?|你(?:的)?)?(?:简历)?(?:已)?(?:投递|提交|申请)(?:已)?成功[！!]?", first)
    if not match:
        return None
    rest = label[len(first):]
    if re.search(r"(?:已|正在|当前|目前).{0,8}(?:笔试|面试|终面|录用|offer|淘汰|拒绝|撤回)|未通过|流程(?:已)?结束", rest, re.I):
        return None
    return first


def status_is_unasserted(label: str, status: str) -> bool:
    """Match bounded explicit negation/condition patterns, not arbitrary '未'.

An exemption for a different stage does not negate an asserted current stage:
``已获得offer，无需笔试`` still supports offer; ``笔试未通过`` supports rejection.
"""
    if status == "applied" and asserted_submission_label(label):
        return False
    if re.search(r"如果|假如|倘若|一旦|或许|可能|若(?:通过|完成|进入|获得)|"
                 r"(?:尚|暂|仍|还)未通过|\b(?:if|unless|provided\s+that|assuming)\b", label, re.I):
        return True
    if re.search(r"(?:通过|完成|结束|合格|成功|面试|笔试|终面)(?:之)?后\s*"
                 r"(?!(?:已|已经|现已|目前已))[^，。；;,.!?！？\n]{0,200}(?:进入|安排|参加|开始|发放|获得|收到|录用|签约)|"
                 r"\b(?:after|upon|following)\s+(?:passing|completing|completion|success)\b", label, re.I):
        return True
    words = _STAGE_WORDS.get(status)
    if not words:
        return False
    target = rf"(?:{words})"
    negative = (r"(?:尚未|暂未|仍未|还未|并未|从未|未曾|未能|未(?!通过)|无需|无须|毋须|"
                r"不需要|不必|不用|免于|没有|并无|暂无|尚无|不会|不予|不再|"
                r"不(?:安排|进行|提供|发放|获得|参加|邀请)|无(?=面试|笔试|offer))")
    if re.search(negative + _CLAUSE_GAP + target, label, re.I):
        return True
    # A negative predicate can follow its subject: 面试未安排 / interview not scheduled.
    if re.search(target + _CLAUSE_GAP + r"(?:尚未|暂未|未安排|未开始|未进行|未获得|未发放|未收到|"
                 r"不需要|无需|取消|\b(?:not|never|no\s+longer|isn't|wasn't|won't)\b)", label, re.I):
        return True
    english_negative = r"\b(?:not|no|never|without|isn't|wasn't|aren't|weren't|hasn't|haven't|didn't|doesn't|don't|won't|can't|cannot)\b"
    if re.search(english_negative + _CLAUSE_GAP + target, label, re.I):
        return True
    future = r"(?:即将|计划|预计|拟于|后续|下一步|将(?:会)?|\b(?:will|would|might|could)\b|\bmay\s+(?:be|enter|receive|get|have)\b)"
    if re.search(future + _CLAUSE_GAP + target, label, re.I):
        return True
    if re.search(target + _CLAUSE_GAP + r"\b(?:will|would|might|could)\b", label, re.I):
        return True
    return False


def is_resume_routing_status(label: str) -> bool:
    """An explicit ATS processing baseline, not a generic 'in progress' mention."""
    label = re.sub(r"^" + _CURRENT_PREFIX, "", label.strip())
    return bool(re.fullmatch(r"分配简历\s*[-—–:：]\s*流程中", label.strip()))


def noncanonical_status_reason(label: str) -> str | None:
    """Preserve meaningful current labels that do not establish a pipeline stage.

    Use complete, asserted labels only. A separately explicit rejection, offer,
    etc. still takes the normal evidence path rather than being hidden here.
    """
    label = label.strip().rstrip("。.!！")
    has_current_prefix = bool(re.match(r"^" + _CURRENT_PREFIX, label))
    label = re.sub(r"^" + _CURRENT_PREFIX, "", label)
    if has_current_prefix and is_resume_routing_status(label):
        return "record_present_status_unknown"
    if re.fullmatch(r"(?:流程中|进行中)", label):
        return "record_present_status_unknown"
    if re.fullmatch(r"(?:已)?(?:推荐|转入|转到|转至)(?:到)?其他(?:职位|岗位)", label):
        return "position_recommendation_unmapped"
    if re.fullmatch(r"(?:已)?(?:归入|转入|进入)(?:公司)?人才库", label):
        return "talent_pool_status_unmapped"
    return None


def literal_record_status(text: str) -> tuple[str, str] | None:
    """Closed literal assertions inside an already uniquely owned record card.

    Submission needs a dated action or an explicit success phrase, not a button.
    A generic ladder is not a later state. Conflicting assertions fail closed.
    This is not suitable for unscoped whole-page text or arbitrary instructions.
    """
    date = _SUBMISSION_DATE
    # OCR may flatten a card into one line or split an action/date across lines.
    # Still require a literal action adjacent to its date, not any date on page.
    submitted = list(_dated_submission_matches(text))
    current_labels = literal_current_status_labels(text)
    if (any(re.fullmatch(_COMPOUND_WRITTEN_LABEL, label, re.I) for label in current_labels)
            and status_is_unasserted(text, "written")):
        # This closed compound is current only inside an asserted status field,
        # not a negated or future statement that happens to contain that field.
        return None
    if (literal_current_status_conflict(text)
            or any(noncanonical_status_reason(label) or is_resume_routing_status(label)
                   for label in current_labels)):
        # A real current-but-unmapped anchor must not disappear behind its old
        # submission date. It neither establishes a later stage nor a conflict.
        return None
    # A closed ATS current-progress phrase can wrap its final “中” onto the
    # next line. Recover that literal phrase, never the completed assessment
    # badge beside it or arbitrary text between cards.
    screening = list(_LITERAL_CURRENT_LABEL.finditer(text))
    for match in screening:
        # A closed label must not hide its immediately following qualification.
        # In particular, newly recognized screening text can still be negated.
        suffix = re.match(r"\s*(?:未通过|淘汰|拒绝|已撤回|尚未|暂未|未安排|未开始|未进行|"
                          r"取消|计划|预计|将会)[^\n，。；;]{0,60}", text[match.end():])
        if suffix:
            qualified = match["label"] + suffix[0]
            status = explicit_label_status(match["label"])
            if status_is_unasserted(qualified, status) or explicit_label_status(qualified) != status:
                return None
    terminal = list(re.finditer(r"(?:^|(?<=\s))(?P<label>(?:流程|申请流程)(?:已)?(?:结束|终止)"
        r"(?:[，,；;\s]*(?:已)?(?:归入|进入|转入)(?:公司)?人才库)?[。.!！]?)(?=$|\s)", text))
    if re.search(r"如果|假如|尚未|暂未|将会|预计|流程未|流程尚未|未终止", text):
        return None
    if terminal:
        last = terminal[-1]
        # Do not infer rejection from an older event preceding a new submission.
        if any(match.start() > last.start() for match in submitted):
            return None
        if any(match.start() > last.start() for match in screening):
            return None
        if re.search(r"(?:已|当前|目前|正在).{0,8}(?:面试|笔试|offer|录用)|(?:面试|笔试|初筛|筛选)(?:进行)?中",
                     text[last.end():], re.I):
            return None
        return "rejected", last.group('label')
    # One closed ATS event sequence observed in the live AAC card. Match it
    # before treating its separately dated event lines as current assertions.
    sequence = re.search(r"(?<!\S)投递简历\s+(?P<submitted>" + date + r")\s+评估中\s+"
                         r"(?P<screened>" + date + r")\s+(?P<label>笔试中)\s+"
                         r"(?P<written>" + date + r")(?=$|\s|[，。；;])", text)
    if sequence and not current_labels:
        dates = []
        for field in ("submitted", "screened", "written"):
            parts = [int(value) for value in re.findall(r"\d+", sequence[field])]
            try:
                dates.append(datetime(*(parts + [0] * (6 - len(parts)))))
            except ValueError:
                return None
        rest = text[:sequence.start()] + text[sequence.end():]
        if (dates == sorted(dates) and not re.search(
                r"笔试|面试|复试|终面|offer|录用|未通过|淘汰|拒绝|撤回|(?:流程|申请流程)(?:已)?(?:终止|结束)|"
                r"计划|后续|下一步|即将|预计|将(?:会)?|如果|假如", rest, re.I)):
            return "written", sequence["label"]
        return None
    assertions = [(explicit_label_status(label), label) for label in current_labels]
    submissions = _dated_submission_labels(text)
    # OCR can flatten adjacent completed-submission and pending-screening cells.
    # This proves only submission; any separately asserted later state below
    # still wins, and inactive interview/offer cells never become current.
    pending_screening = re.search(
        r"(?<!\S)(?P<label>简历投递\s*成功)\s+筛选\s*待评估(?=$|\s|[，。；;])", text)
    if pending_screening and not status_is_unasserted(text, "applied"):
        submissions.append(pending_screening["label"])
    lines = text.splitlines()
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        # Some ATS cards prepend a channel tag to the dated submission action.
        # Strip only this closed metadata vocabulary, never arbitrary page text.
        line = re.sub(r"^(?:校园招聘|社会招聘|校招|社招)\s*[|｜·]\s*(?=" + date + r")", "", line)
        label = re.sub(r"\s*" + date + r"\s*$", "", line).strip()
        label = re.sub(r"^" + date + r"\s*", "", label).strip()
        if re.fullmatch(r"(?:流程|申请流程)(?:已)?(?:结束|终止)(?:[，,；;\s]*(?:已)?(?:归入|进入|转入)(?:公司)?人才库)?[。.!！]?", label):
            assertions.append(("rejected", label))
        elif not _dated_history_line(lines, index) and re.fullmatch(r"(?:简历)?(?:筛选|初筛|测评|笔试|面试|复试|终面)(?:\s*[·:：-]?\s*进行)?中|(?:筛选\s*)?待评估", label):
            assertions.append((explicit_label_status(label), label))
        if dated_submission_label(line) or asserted_submission_label(line):
            submissions.append(line)
    # Unknown/negated/different outcome text must never be turned into applied.
    if len({stage for stage, _ in assertions}) > 1:
        return None
    if assertions:
        status, label = assertions[-1]
        return status, label
    if submissions and not re.search(r"未通过|淘汰|拒绝|(?:流程|申请流程)(?:已)?(?:终止|结束)|申请终止|已(?:终止|结束)|撤回|未投递|投递失败|待投递|尚未|将会|如果|(?:当前|目前|最新状态|已|正在).{0,6}(?:笔试|面试|offer|录用)", text, re.I):
        return "applied", submissions[-1]
    return None


def explicit_label_status(label: str) -> str | None:
    """Shared semantic check for model proposals and the independent verifier."""
    if dated_submission_label(label):
        return "applied"
    if re.match(r"^(?:" + _SUBMISSION_METADATA + r"|" + _SUBMISSION_ACTION
                + r"\s+20\d{2}|20\d{2}[年./-])", label.strip()):
        # An incomplete/invalid date must not fall through to the word '投递'.
        return None
    if re.fullmatch(r"官网(?:主)?投递|内推(?:投递)?|投递渠道|投递来源", label.strip()):
        # Source-channel badges are not current application stages.
        return None
    if noncanonical_status_reason(label):
        return None
    if asserted_submission_label(label):
        return "applied"
    canonical = label.strip().casefold()
    if canonical in {"applied", "written", "interview", "hr", "offer", "rejected", "withdrawn"}:
        return canonical
    for status, pattern in (
        ("withdrawn", r"已撤回|撤回成功|已取消申请|withdrawn"),
        ("rejected", r"淘汰|不合适|未通过|暂不匹配|不匹配|流程(?:已)?(?:终止|结束)|申请终止|拒绝|rejected|unsuccessful"),
        ("offer", r"offer|已录用|拟录用|录用通知|待入职|已入职|签约"),
        ("hr", r"hr\s*面|人力面|终面|终试|洽谈"),
        ("interview", r"面试|初试|复试|一面|二面|三面|interview"),
        ("written", r"笔试|机试|编程测试|在线考试|written\s*test|coding\s*test"),
        ("applied", r"筛选|初筛|简历复筛|简历评估|待评估|评估中|投递|申请成功|已申请|处理中|待处理|等待处理|测评|screening|submitted|under\s*review"),
    ):
        if re.search(pattern, label, re.I):
            return None if status_is_unasserted(label, status) else status
    return None
