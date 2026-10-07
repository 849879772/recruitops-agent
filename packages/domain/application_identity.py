"""Conservative identity matching for captured application cards.

Punctuation is identity data: C++, C#, and single/double hyphens must not collapse.
Only documented presentation affixes are removed, after exact matching fails.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from packages.domain.urls import normalize_http_page_url


def title_key(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


_TRANSFER_SUFFIX = re.compile(r"\s*\(\s*(?:接受调剂|服从调剂)\s*\)\s*$")


def _without_transfer_suffix(value: object) -> str:
    # These are application preferences, not arbitrary role/location qualifiers.
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return _TRANSFER_SUFFIX.sub("", text, count=1)


def clean_display_title(value: object) -> str:
    text = _without_transfer_suffix(value)
    text = re.sub(r"^NO\.?\s*\d{2,}\s*", "", text, flags=re.I)
    text = re.sub(r"\s*(?:网申)?第\s*[一二三四五六七八九十\d]+\s*志愿\s*$", "", text)
    return title_key(_without_transfer_suffix(text))


def title_span_is_complete(context: str, start: int, end: int) -> bool:
    """A role cannot be a word fragment or hide a parenthesized qualifier."""
    return not (start and re.match(r"[\w\u4e00-\u9fff]", context[start - 1])) and not (
        end < len(context) and re.match(r"[\w\u4e00-\u9fff（(]", context[end])
    )


def title_in_context(title: object, context: object) -> bool:
    # Retain original boundaries while tolerating presentation within a title.
    # Compacting the whole context first turns AI测试开发工程师 into two owners.
    target, original = title_key(title), str(context or "")
    if not target:
        return False
    characters, offsets = [], []
    for index, character in enumerate(original):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for value in normalized:
            if not value.isspace():
                characters.append(value)
                offsets.append(index)
    text, cursor = "".join(characters), 0
    while (start := text.find(target, cursor)) >= 0:
        if title_span_is_complete(original, offsets[start], offsets[start + len(target) - 1] + 1):
            return True
        cursor = start + 1
    return False


def is_application_title(value: object) -> bool:
    """Bare preference/component headings are not the application's job title."""
    key = title_key(value)
    return bool(key) and not any(re.fullmatch(pattern, key) for pattern in (
        r"(?:网申)?第[一二三四五六七八九十\d]+(?:志愿|意向)(?:已激活|未激活)?",
        # A conditional process notice is not a second application identity.
        # Keep this grammatical form bounded instead of excluding titles that
        # merely contain 笔试, 信息技术 or 岗位.
        r"仅部分[^。！？!?]{0,40}(?:类)?岗位(?:需|需要|须)(?:进行|参加)(?:笔试|测评)[。.!！]?",
        # Some ATS cards expose this process component as their first heading.
        # Exclude the complete label, never job titles containing 测试 or AI.
        r"笔试/ai语言测试",
    ))


def _value(application: object, name: str) -> str:
    value = application.get(name) if isinstance(application, Mapping) else getattr(application, name, None)
    return str(value or "").strip()


_COHORT_PREFIX = re.compile(
    r"^(?:(?:【|\[)(?:(?P<bracket_year>20\d{2}|\d{2})(?:届(?:校招|校园招聘)?|校招|校园招聘)|校招|校园招聘|秋招|春招)(?:】|\])"
    r"|(?P<year>20\d{2}|\d{2})(?:届(?:校招|校园招聘)?|校招|校园招聘)|校招|校园招聘|秋招|春招)\s*[-:：]?\s*"
)
_COHORT_SUFFIX = re.compile(r"-(?P<year>20\d{2}|\d{2})届(?:秋招|春招|校招)$")

# Only a delimited, literal city is a display suffix. Direction names, ATS job
# numbers and arbitrary parenthesized text remain part of the job identity.
_DISPLAY_CITIES = (
    "北京", "上海", "深圳", "广州", "成都", "杭州", "南京", "武汉", "西安", "苏州",
    "合肥", "重庆", "天津", "长沙", "东莞", "佛山", "无锡", "郑州", "济南", "青岛",
    "宁波", "厦门", "福州", "珠海", "惠州", "中山", "南昌", "大连", "沈阳", "长春",
    "哈尔滨", "石家庄", "太原", "昆明", "贵阳", "南宁", "海口", "乌鲁木齐", "兰州",
    "银川", "西宁", "呼和浩特", "拉萨", "香港", "澳门",
)
_CITY_SUFFIX = re.compile(
    r"(?:-(?P<hyphen>" + "|".join(_DISPLAY_CITIES)
    + r")市?|\((?P<bracket>" + "|".join(_DISPLAY_CITIES) + r")市?\))$"
)
_VOLUNTEER_SUFFIX = re.compile(r"(?:网申)?第(?P<number>[一二三四五六七八九十\d]+)志愿$")


def displayed_cities(value: object) -> frozenset[str]:
    """Closed city tokens for rejecting contradictory same-capture OCR anchors."""
    text = str(value or "")
    return frozenset(city for city in _DISPLAY_CITIES if city in text)


def _volunteer(value: object) -> str:
    match = _VOLUNTEER_SUFFIX.search(title_key(_without_transfer_suffix(value)))
    if not match:
        return ""
    number = match.group("number")
    return str(int(number)) if number.isdigit() else str({
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }.get(number, number))


def _volunteer_agrees(target: object, candidate: object) -> bool:
    # Preserve a volunteer when both sides state one. A canonical title returned
    # by the scoped evidence verifier can omit display tags; repeated cards still
    # return multiple matches and must be confirmed by the caller.
    number, candidate_number = _volunteer(target), _volunteer(candidate)
    return not number or not candidate_number or number == candidate_number


def _location_title(value: object) -> tuple[str, frozenset[str], str]:
    text, years = _recruitment_title(value)
    match = _CITY_SUFFIX.search(text)
    if not match:
        return text, years, ""
    return text[:match.start()], years, match.group("hyphen") or match.group("bracket")


def _recruitment_title(value: object) -> tuple[str, frozenset[str]]:
    """Remove closed recruitment tags, never job/location qualifiers.

    At most one separator is part of a tag: 27届-C++ and 27届--C++ remain
    distinct. A year on both sides must agree, even when their job names match.
    """
    text = clean_display_title(value)
    years = set()
    for _ in range(2):
        match = _COHORT_PREFIX.match(text)
        if not match:
            break
        year = match.group("bracket_year") or match.group("year")
        if year:
            years.add(year if len(year) == 4 else "20" + year)
        text = text[match.end():]
    suffix = _COHORT_SUFFIX.search(text)
    if suffix:
        year = suffix.group("year")
        years.add(year if len(year) == 4 else "20" + year)
        text = text[:suffix.start()]
    if len(years) > 1:
        # Contradictory leading/trailing cohorts cannot normalize into one role.
        return "", frozenset(years)
    return text, frozenset(years)


def recruitment_titles_agree(left: object, right: object) -> bool:
    """Closed recruitment tags only; do not remove cities, directions or IDs."""
    left_title, left_years = _recruitment_title(left)
    right_title, right_years = _recruitment_title(right)
    return bool(left_title) and left_title == right_title and not (
        left_years and right_years and left_years != right_years
    ) and _volunteer_agrees(left, right)


def _verified_binding_records(application: object, records: Sequence[Mapping]) -> list[Mapping] | None:
    hints = (application.get("verified_identity_bindings") if isinstance(application, Mapping)
             else getattr(application, "verified_identity_bindings", None))
    page_url = normalize_http_page_url(_value(application, "record_url"))
    if not page_url or not isinstance(hints, (list, tuple)):
        return None
    matched = []
    valid_binding = False
    for hint in hints:
        if (not isinstance(hint, Mapping) or hint.get("verified") is not True
                or str(hint.get("application_id") or "") != _value(application, "id")
                or normalize_http_page_url(str(hint.get("page_url") or "")) != page_url):
            continue
        raw_title = title_key(hint.get("raw_title"))
        if not raw_title:
            continue
        valid_binding = True
        for record in records:
            if title_key(record.get("raw_title") or record.get("title")) != raw_title:
                continue
            if any(str(record.get(card_field) or "") != str(hint[hint_field])
                   for hint_field, card_field in (("external_application_id", "application_id"), ("external_job_id", "job_id"))
                   if hint.get(hint_field)):
                continue
            if not any(item is record for item in matched):
                matched.append(record)
    return matched if valid_binding else None


def matching_records(application: object, records: Sequence[Mapping]) -> list[Mapping]:
    """Return the strongest matching tier; callers must require exactly one."""
    records = [record for record in records
               if is_application_title(record.get("raw_title") or record.get("title"))]
    # A human-confirmed identity is a constraint, not a fuzzy alias. A changed
    # website identifier must not silently fall back to a similarly named card.
    verified = _verified_binding_records(application, records)
    if verified is not None:
        return verified
    # Page application_id/job_id belong to the ATS, not our local snapshot IDs.
    # Only compare them when the stored record explicitly carries that namespace.
    for field, target in (("application_id", _value(application, "external_application_id")),
                          ("job_id", _value(application, "external_job_id"))):
        if target:
            matched = [record for record in records if str(record.get(field) or "").strip() == target]
            if matched:
                return matched
    title = _value(application, "job_title")
    if not title_key(title):
        return []
    exact = [record for record in records
             if title_key(record.get("raw_title") or record.get("title")) == title_key(title)]
    if exact:
        return exact
    cleaned = clean_display_title(title)
    target, years = _recruitment_title(title)
    matches = [record for record in records
               if cleaned and clean_display_title(record.get("raw_title") or record.get("title")) == cleaned
               and _volunteer_agrees(title, record.get("raw_title") or record.get("title"))
               and not (years and (record_years := _recruitment_title(record.get("raw_title") or record.get("title"))[1])
                        and years != record_years)]
    if matches:
        return matches
    matched = []
    for record in records:
        raw_title = record.get("raw_title") or record.get("title")
        candidate, candidate_years = _recruitment_title(raw_title)
        if (target and candidate == target and _volunteer_agrees(title, raw_title)
                and not (years and candidate_years and years != candidate_years)):
            matched.append(record)
    if matched:
        return matched
    target, years, city = _location_title(title)
    for record in records:
        raw_title = record.get("raw_title") or record.get("title")
        candidate, candidate_years, candidate_city = _location_title(raw_title)
        if (target and candidate == target and (not city or city == candidate_city)
                and _volunteer_agrees(title, raw_title)
                and not (years and candidate_years and years != candidate_years)):
            matched.append(record)
    return matched


def unique_record(application: object, records: Sequence[Mapping]) -> Mapping | None:
    matches = matching_records(application, records)
    return matches[0] if len(matches) == 1 else None
