from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail.identity import (
    IdentityMatchStatus,
    company_names_match,
    find_unique_application_match,
    job_titles_match,
    mail_matches_application,
    match_application_identity,
    normalize_job_title,
)
from packages.recruitment_mail.models import (
    CompanyCandidate,
    JobCandidate,
    MailIdentity,
    ParsedRecruitmentEmail,
    RecruitmentMessageCategory,
)


def _application(identifier: str, company: str, title: str) -> Application:
    return Application(
        id=identifier,
        company_name=company,
        job_title=title,
        stage=ApplicationStage.APPLIED,
        idempotency_key=f"application:{identifier}",
        source="fixture",
        source_ref=identifier,
    )


def test_dahua_alias_is_controlled_and_not_substring_matching() -> None:
    assert company_names_match("大华股份", "浙江大华技术股份有限公司")
    assert not company_names_match("大华", "浙江大华技术股份有限公司")
    assert not company_names_match("大华股份", "大华股份有限公司")


def test_dahua_job_ignores_explicit_ats_code_but_keeps_department() -> None:
    numbered = "【研发中心】2027届具身智能算法工程师(J24413)"
    email_title = "【研发中心】2027届具身智能算法工程师"

    assert normalize_job_title(numbered) == normalize_job_title(email_title)
    assert job_titles_match(numbered, email_title)
    assert not job_titles_match(
        numbered,
        "【智能制造中心】2027届具身智能算法工程师(J24413)",
    )


def test_job_identity_keeps_cpp_and_csharp_distinct() -> None:
    assert not job_titles_match("C++工程师", "C#工程师")


def test_dahua_identity_selects_one_candidate() -> None:
    target = _application(
        "application-dahua-robot",
        "浙江大华技术股份有限公司",
        "【研发中心】2027届具身智能算法工程师",
    )
    candidates = [
        target,
        _application(
            "application-dahua-other-dept",
            "浙江大华技术股份有限公司",
            "【智能制造中心】2027届具身智能算法工程师",
        ),
        _application("application-other-company", "另一家公司", "具身智能算法工程师"),
    ]

    result = match_application_identity(
        candidates,
        company_name="大华股份",
        job_title="【研发中心】2027届具身智能算法工程师(J24413)",
    )

    assert result.status is IdentityMatchStatus.UNIQUE
    assert result.application is target
    assert result.candidates == (target,)
    assert find_unique_application_match(
        candidates,
        company_name="大华股份",
        job_title="【研发中心】2027届具身智能算法工程师(J24413)",
    ) is target


def test_identity_does_not_select_duplicate_candidates() -> None:
    candidates = [
        _application("application-1", "浙江大华技术股份有限公司", "2027届具身智能算法工程师"),
        _application("application-2", "浙江大华技术股份有限公司", "2027届具身智能算法工程师"),
    ]

    result = match_application_identity(
        candidates,
        company_name="大华股份",
        job_title="2027届具身智能算法工程师(J24413)",
    )

    assert result.status is IdentityMatchStatus.AMBIGUOUS
    assert result.application is None
    assert len(result.candidates) == 2
    assert find_unique_application_match(
        candidates,
        company_name="大华股份",
        job_title="2027届具身智能算法工程师(J24413)",
    ) is None


def test_mail_matches_dahua_full_department_title_with_unnumbered_candidate() -> None:
    application = _application(
        "application-dahua-robot",
        "浙江大华技术股份有限公司",
        "【研发中心】2027届具身智能算法工程师(J24413)",
    )
    message = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-dahua-rejection"),
        subject="大华股份辞谢信",
        body_text="很遗憾，您没有通过【研发中心】2027届具身智能算法工程师的简历筛选。",
        category=RecruitmentMessageCategory.REJECTION,
        company_candidates=[CompanyCandidate(value="大华股份", evidence="主题", confidence=1.0)],
        job_candidates=[
            JobCandidate(value="2027届具身智能算法工程师", evidence="正文", confidence=1.0)
        ],
        confidence=1.0,
    )

    assert mail_matches_application(message, application)


def test_mail_match_rejects_wrong_department_and_partial_job_candidate() -> None:
    application = _application(
        "application-dahua-robot",
        "浙江大华技术股份有限公司",
        "【研发中心】2027届具身智能算法工程师(J24413)",
    )
    wrong_department = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-dahua-wrong-dept"),
        subject="大华股份通知",
        body_text="【智能制造中心】2027届具身智能算法工程师的简历筛选结果",
        category=RecruitmentMessageCategory.REJECTION,
        company_candidates=[CompanyCandidate(value="大华股份", confidence=1.0)],
        job_candidates=[JobCandidate(value="2027届具身智能算法工程师", confidence=1.0)],
        confidence=1.0,
    )
    partial_candidate = wrong_department.model_copy(
        update={
            "identity": MailIdentity(message_id="mail-dahua-partial"),
            "body_text": "【研发中心】2027届具身智能算法工程师的简历筛选结果",
            "job_candidates": [JobCandidate(value="具身智能算法工程师", confidence=1.0)],
        }
    )

    assert not mail_matches_application(wrong_department, application)
    assert not mail_matches_application(partial_candidate, application)
