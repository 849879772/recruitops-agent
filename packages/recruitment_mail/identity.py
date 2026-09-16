"""Deterministic company and job identity matching for mail-bound applications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata

from packages.domain.models import Application

from .models import ParsedRecruitmentEmail


class IdentityMatchStatus(StrEnum):
    """Outcome of matching one mail identity against application candidates."""

    NO_MATCH = "no_match"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ApplicationIdentityMatch:
    """A side-effect-free identity resolution result."""

    status: IdentityMatchStatus
    application: Application | None = None
    candidates: tuple[Application, ...] = ()

    @property
    def is_unique(self) -> bool:
        return self.status is IdentityMatchStatus.UNIQUE


def normalize_company_name(value: str) -> str:
    """Normalize formatting without removing legal-name content or inferring aliases."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or "\u3400" <= character <= "\u9fff"
    )


# Aliases are deliberately enumerated. Matching never uses substring or character-set rules.
_CONTROLLED_COMPANY_ALIAS_GROUPS = (
    frozenset(normalize_company_name(name) for name in (
        "达梦", "达梦数据库", "武汉达梦数据库股份有限公司")),
    frozenset(normalize_company_name(name) for name in (
        "奇瑞", "奇瑞汽车", "奇瑞汽车股份有限公司")),
    frozenset(
        normalize_company_name(name)
        for name in (
            "浙江大华技术股份有限公司",
            "大华股份",
        )
    ),
    frozenset(
        normalize_company_name(name)
        for name in (
            "科大讯飞",
            "科大讯飞股份有限公司",
        )
    ),
)


def mail_company_matches_application(
    message: ParsedRecruitmentEmail,
    application: Application,
) -> bool:
    """Match a persisted mail to an application by controlled company identity only."""

    return any(
        company_names_match(candidate.value, application.company_name)
        for candidate in message.company_candidates
    )


def company_names_match(left: str, right: str) -> bool:
    """Return whether two company names are equal or belong to one controlled alias group."""

    left_key = normalize_company_name(left)
    right_key = normalize_company_name(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    def legal_stem(key):
        return re.sub(r"(?:股份有限公司|有限责任公司|有限公司)$", "", key)
    if len(legal_stem(left_key)) >= 3 and legal_stem(left_key) == legal_stem(right_key):
        return True
    return any(
        left_key in group and right_key in group
        for group in _CONTROLLED_COMPANY_ALIAS_GROUPS
    )


_ATS_CODE_SUFFIX = re.compile(
    r"""
    (?:
        [\s_-]*[\(\[\{【（〔]
        \s*[A-Za-z]{1,8}[-_]?\d{3,12}\s*
        [\)\]\}】）〕]
        |
        [\s_-]+[A-Za-z]{1,8}[-_]?\d{3,12}
    )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_JOB_IDENTITY_PUNCTUATION = frozenset("+#/&-.")


def strip_ats_job_code(value: str) -> str:
    """Remove one explicit trailing ATS code such as ``(J24413)``."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    return _ATS_CODE_SUFFIX.sub("", normalized).rstrip()


def normalize_job_title(value: str) -> str:
    """Normalize job formatting while retaining departments and C++/C# distinctions."""

    normalized = unicodedata.normalize("NFKC", strip_ats_job_code(value)).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum()
        or "\u3400" <= character <= "\u9fff"
        or character in _JOB_IDENTITY_PUNCTUATION
    )


def job_titles_match(left: str, right: str) -> bool:
    """Return whether titles are exactly equal after safe ATS-code normalization."""

    left_key = normalize_job_title(left)
    right_key = normalize_job_title(right)
    return bool(left_key and right_key and left_key == right_key)


_DEPARTMENT_PREFIX = re.compile(
    r"(?P<open>[\[【])\s*(?P<department>[^\]】]{1,100}?)\s*[\]】]"
)
_TITLE_FOLLOWING_MARKERS = (
    "的",
    "之",
    "岗位",
    "职位",
    "招聘",
    "简历",
    "面试",
    "笔试",
    "测评",
    "通知",
    "申请",
    "筛选",
    "结果",
    "进展",
    "工作",
)
_NORMALIZED_ATS_CODE = re.compile(r"[a-z]{1,8}\d{3,12}", re.IGNORECASE)


def _job_title_parts(value: str) -> tuple[str | None, str]:
    """Return an explicit square-bracket department and its complete title."""

    text = unicodedata.normalize("NFKC", strip_ats_job_code(value)).strip()
    prefix = _DEPARTMENT_PREFIX.match(text)
    if prefix is None:
        return None, normalize_job_title(text)
    department = normalize_job_title(prefix.group("department")) or None
    title = normalize_job_title(text[prefix.end():])
    return department, title


def _contains_department_title(
    text: str,
    title: str,
    *,
    department: str,
) -> bool:
    """Find a complete title immediately following the expected explicit department."""

    normalized_text = unicodedata.normalize("NFKC", text)
    for prefix in _DEPARTMENT_PREFIX.finditer(normalized_text):
        observed_department = normalize_job_title(prefix.group("department"))
        if observed_department != department:
            continue
        following = normalize_job_title(normalized_text[prefix.end():])
        if not following.startswith(title):
            continue
        remainder = following[len(title):]
        if not remainder or remainder.startswith(_TITLE_FOLLOWING_MARKERS):
            return True
        if _NORMALIZED_ATS_CODE.fullmatch(remainder):
            return True
    return False


def mail_matches_application(
    message: ParsedRecruitmentEmail,
    application: Application,
) -> bool:
    """Match persisted mail identity to one application without fuzzy candidate lookup."""

    if not mail_company_matches_application(message, application):
        return False

    application_title = normalize_job_title(application.job_title)
    application_department, application_base_title = _job_title_parts(application.job_title)
    if not application_title or not application_base_title:
        return False

    mail_text = f"{message.subject}\n{message.body_text}"
    normalized_mail_text = normalize_job_title(mail_text)
    for candidate in message.job_candidates:
        candidate_department, candidate_base_title = _job_title_parts(candidate.value)
        if not candidate_base_title or not job_titles_match(
            candidate.value,
            application.job_title,
        ):
            # A missing department prefix is handled only by the explicit-context branch below.
            if not candidate_base_title or not job_titles_match(
                candidate_base_title,
                application_base_title,
            ):
                continue
            if application_department is None and candidate_department is None:
                continue
            if application_department is not None:
                if (
                    candidate_department is not None
                    and candidate_department != application_department
                ):
                    continue
                if _contains_department_title(
                    mail_text,
                    application_base_title,
                    department=application_department,
                ):
                    return True
            elif candidate_department is not None and _contains_department_title(
                mail_text,
                application_base_title,
                department=candidate_department,
            ):
                return True
            continue
        if application_title in normalized_mail_text:
            return True
    return False


def match_application_identity(
    applications: Sequence[Application],
    *,
    company_name: str,
    job_title: str,
) -> ApplicationIdentityMatch:
    """Match a company/title pair and select an application only when it is unique."""

    if not normalize_company_name(company_name) or not normalize_job_title(job_title):
        return ApplicationIdentityMatch(status=IdentityMatchStatus.NO_MATCH)

    candidates = tuple(
        application
        for application in applications
        if company_names_match(company_name, application.company_name)
        and job_titles_match(job_title, application.job_title)
    )
    if len(candidates) == 1:
        return ApplicationIdentityMatch(
            status=IdentityMatchStatus.UNIQUE,
            application=candidates[0],
            candidates=candidates,
        )
    if len(candidates) > 1:
        return ApplicationIdentityMatch(
            status=IdentityMatchStatus.AMBIGUOUS,
            candidates=candidates,
        )
    return ApplicationIdentityMatch(status=IdentityMatchStatus.NO_MATCH)


def find_unique_application_match(
    applications: Sequence[Application],
    *,
    company_name: str,
    job_title: str,
) -> Application | None:
    """Return the sole matching application, or ``None`` for no/ambiguous matches."""

    result = match_application_identity(
        applications,
        company_name=company_name,
        job_title=job_title,
    )
    return result.application if result.is_unique else None


__all__ = [
    "ApplicationIdentityMatch",
    "IdentityMatchStatus",
    "company_names_match",
    "find_unique_application_match",
    "job_titles_match",
    "mail_company_matches_application",
    "mail_matches_application",
    "match_application_identity",
    "normalize_company_name",
    "normalize_job_title",
    "strip_ats_job_code",
]
