from datetime import datetime, timezone

from packages.domain.models import Application, ApplicationStage
from packages.tools.application_review import (
    ApplicationStatusReviewInput,
    ObservedApplicationStatus,
    ReviewMode,
    application_status_review,
    normalize_application_page_url,
)
from packages.tools.typed import ToolStatus


def test_application_page_url_preserves_safe_spa_route_but_strips_secrets() -> None:
    value = (
        "https://app.mokahr.com/campus-recruitment/acme/123"
        "?locale=zh-CN&sessionid=secret#/candidateHome/applications?tab=current"
    )

    assert normalize_application_page_url(value) == (
        "https://app.mokahr.com/campus-recruitment/acme/123#/candidateHome/applications"
    )
    assert normalize_application_page_url("https://example.com/path#marketing-anchor") == (
        "https://example.com/path"
    )
    assert normalize_application_page_url("https://user:secret@example.com/applications") is None


class ApplicationRepository:
    def __init__(self, applications: list[Application]) -> None:
        self.applications = applications

    def list_applications(self) -> list[Application]:
        return self.applications


def _application(
    application_id: str,
    title: str,
    *,
    stage: ApplicationStage = ApplicationStage.APPLIED,
    record_url: str | None = "https://ats.example.com/applications?token=secret",
) -> Application:
    return Application(
        id=application_id,
        company_name="示例公司",
        job_title=title,
        record_url=record_url,
        stage=stage,
        idempotency_key=f"application:{application_id}",
        source="fixture-applications",
        source_ref=application_id,
    )


def test_review_plan_deduplicates_pages_and_reports_missing_urls() -> None:
    repository = ApplicationRepository(
        [
            _application("1", "C++ 开发工程师"),
            _application("2", "算法工程师", record_url="https://ats.example.com/applications?page=2"),
            _application("3", "测试开发工程师", record_url=None),
        ]
    )

    response = application_status_review(
        ApplicationStatusReviewInput(review_id="review-1"), repository
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert response.data.mode is ReviewMode.PLAN
    assert response.data.applications_total == 3
    assert response.data.pages_total == 1
    assert len(response.data.targets[0].applications) == 2
    assert response.data.targets[0].normalized_url == "https://ats.example.com/applications"
    assert response.data.unresolved[0].reason == "record_url_missing_or_invalid"


def test_review_reconciles_multi_job_page_and_builds_forward_only_preview() -> None:
    repository = ApplicationRepository(
        [
            _application("1", "C++ 开发工程师"),
            _application("2", "算法工程师", stage=ApplicationStage.WRITTEN),
        ]
    )
    plan = application_status_review(
        ApplicationStatusReviewInput(review_id="review-2"), repository
    )
    target = plan.data.targets[0]

    response = application_status_review(
        ApplicationStatusReviewInput(
            review_id="review-2",
            observations=[
                {
                    "target_id": target.target_id,
                    "url": "https://ats.example.com/applications?session=hidden",
                    "captured_at": datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
                    "entries": [
                        {
                            "status": "written",
                            "label": "笔试中",
                            "context": "示例公司 C++ 开发工程师 笔试中",
                        },
                        {
                            "status": "interview",
                            "label": "面试",
                            "context": "示例公司 算法工程师 面试",
                        },
                    ],
                }
            ],
        ),
        repository,
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert response.data.mode is ReviewMode.RECONCILE
    assert len(response.data.proposals) == 2
    first = response.data.proposals[0]
    assert first.observed_status is ObservedApplicationStatus.WRITTEN
    assert first.target_stage is ApplicationStage.WRITTEN
    assert first.approval_preview.operation.value == "application_stage_update"
    assert first.approval_preview.payload["application_id"] == 1
    assert "笔试中" in first.approval_preview.evidence_summary
    second = response.data.proposals[1]
    assert second.target_stage is ApplicationStage.INTERVIEW1


def test_review_fails_closed_for_ambiguous_status() -> None:
    repository = ApplicationRepository(
        [
            _application("1", "软件工程师", stage=ApplicationStage.INTERVIEW1),
            _application("2", "软件工程师（平台）"),
        ]
    )
    plan = application_status_review(
        ApplicationStatusReviewInput(review_id="review-3"), repository
    )
    target = plan.data.targets[0]

    response = application_status_review(
        ApplicationStatusReviewInput(
            review_id="review-3",
            observations=[
                {
                    "target_id": target.target_id,
                    "url": target.record_url,
                    "captured_at": datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
                    "entries": [
                        {
                            "status": "applied",
                            "label": "已投递",
                            "context": "软件工程师 软件工程师（平台） 已投递",
                        }
                    ],
                }
            ],
        ),
        repository,
    )

    assert response.data is not None
    assert response.data.proposals == []
    reasons = {item.reason for item in response.data.unresolved}
    assert "status_entry_ambiguous" in reasons


def test_review_fails_closed_for_regressive_status() -> None:
    repository = ApplicationRepository(
        [_application("1", "机械臂工程师", stage=ApplicationStage.INTERVIEW1)]
    )
    plan = application_status_review(
        ApplicationStatusReviewInput(review_id="review-4"), repository
    )
    target = plan.data.targets[0]

    response = application_status_review(
        ApplicationStatusReviewInput(
            review_id="review-4",
            observations=[
                {
                    "target_id": target.target_id,
                    "url": target.record_url,
                    "captured_at": datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
                    "entries": [{"status": "applied", "label": "已投递"}],
                }
            ],
        ),
        repository,
    )

    assert response.data is not None
    assert response.data.proposals == []
    assert response.data.unresolved[0].reason == "stage_regression_or_terminal_conflict"
