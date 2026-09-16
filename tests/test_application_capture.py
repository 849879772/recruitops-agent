from packages.approval import OperationName
from packages.domain.models import Job, RecruitmentBatch
from packages.tools import ApplicationCaptureInput, ToolStatus, prepare_application_capture
from tests.test_typed_tools import InMemoryRepository


def _request(**updates):
    values = {
        "request_id": "capture-1",
        "url": "https://example.com/jobs/1?tracking=removed",
        "title": "C++开发工程师",
        "page_text": "示例公司 C++开发工程师 投递成功",
    }
    values.update(updates)
    return ApplicationCaptureInput(**values)


def test_unique_job_page_creates_an_approval_preview() -> None:
    repository = InMemoryRepository()
    repository.applications = []

    result = prepare_application_capture(_request(), repository)

    assert result.status is ToolStatus.SUCCESS
    assert result.data is not None
    assert result.data.capture_status == "approval_required"
    assert result.data.matched_job.job_id == "job-1"
    preview = result.data.approval_preview
    assert preview is not None
    assert preview.operation is OperationName.APPLICATION_CREATE
    assert preview.payload["current_stage"] == "applied"
    assert preview.cohort == 2027


def test_existing_application_is_not_enqueued_again() -> None:
    result = prepare_application_capture(_request(), InMemoryRepository())

    assert result.status is ToolStatus.NO_RESULTS
    assert result.data is not None
    assert result.data.capture_status == "already_recorded"
    assert result.data.approval_preview is None


def test_shared_page_identity_requires_explicit_job_id() -> None:
    repository = InMemoryRepository()
    repository.applications = []
    repository.jobs.append(
        Job(
            id="job-2",
            company_id="company-1",
            title="机器人算法工程师",
            detail_url="https://example.com/jobs/1?job=2",
            jd_raw="职责与任职要求",
            cohort=2027,
            cohort_status="confirmed",
            batch=RecruitmentBatch.FORMAL,
            source="fixture-jobs",
            source_ref="jobs/job-2",
        )
    )

    ambiguous = prepare_application_capture(_request(), repository)
    explicit = prepare_application_capture(_request(job_id="job-2"), repository)

    assert ambiguous.status is ToolStatus.AMBIGUOUS
    assert {item.job_id for item in ambiguous.data.candidates} == {"job-1", "job-2"}
    assert explicit.status is ToolStatus.SUCCESS
    assert explicit.data.matched_job.job_id == "job-2"


def test_unknown_explicit_job_id_stops_without_guessing() -> None:
    repository = InMemoryRepository()
    repository.applications = []

    result = prepare_application_capture(_request(job_id="invented"), repository)

    assert result.status is ToolStatus.NO_RESULTS
    assert result.data.capture_status == "not_found"
    assert result.data.approval_preview is None
