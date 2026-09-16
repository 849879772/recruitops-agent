from types import SimpleNamespace

import pytest

import packages.scheduler.runtime as scheduler_runtime
from packages.domain.models import ApplicationStage
from packages.recruitment_mail import RecruitmentMailProcessingStatus


class _MailStore:
    def __init__(self, *records):
        self.records = list(records)
        self.status_updates = []

    def query(self, **_kwargs):
        return list(self.records)

    def update_processing_status(self, record_id, processing_status, **kwargs):
        self.status_updates.append((record_id, processing_status, kwargs))


class _Repository:
    def __init__(self, *applications):
        self.applications = list(applications)

    def list_applications(self):
        return list(self.applications)


def _record(status, *, category, application_id="app-iflytek", subject="iflytek mail"):
    return SimpleNamespace(
        id=f"mail-{status}-{category}",
        category=category,
        processing_status=status,
        application_id=application_id,
        subject=subject,
    )


@pytest.mark.parametrize(
    ("status", "category", "stage"),
    [
        (
            RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED.value,
            "application_confirmation",
            ApplicationStage.REJECTED,
        ),
        (
            RecruitmentMailProcessingStatus.PROCESSED_UPDATED.value,
            "rejection",
            ApplicationStage.APPLIED,
        ),
    ],
)
def test_processed_successes_skip_even_when_current_stage_differs(
    monkeypatch, status, category, stage
):
    record = _record(status, category=category)
    store = _MailStore(record)
    repository = _Repository(SimpleNamespace(id="app-iflytek", stage=stage))

    def unexpected(*_args, **_kwargs):
        pytest.fail("processed mail must not be reviewed or updated")

    monkeypatch.setattr(scheduler_runtime, "review_recruitment_mail", unexpected)
    monkeypatch.setattr(scheduler_runtime, "update_application_status", unexpected)

    result = scheduler_runtime._link_unambiguous_recruitment_mail(store, repository)

    assert result == {
        "association_reviewed": 0,
        "association_linked": 0,
        "association_unresolved": 0,
        "approval_previews": 0,
        "updated": 0,
        "unchanged": 0,
        "conflicts": 0,
    }
    assert store.status_updates == []
    assert record.processing_status == status


def test_ambiguous_application_remains_terminal_and_is_not_retried(monkeypatch):
    record = _record(
        RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value,
        category="application_confirmation",
        application_id=None,
    )
    store = _MailStore(record)
    repository = _Repository()

    def unexpected(*_args, **_kwargs):
        pytest.fail("ambiguous application mail must not be retried")

    monkeypatch.setattr(scheduler_runtime, "review_recruitment_mail", unexpected)
    monkeypatch.setattr(scheduler_runtime, "update_application_status", unexpected)

    scheduler_runtime._link_unambiguous_recruitment_mail(store, repository)

    assert store.status_updates == []
    assert record.processing_status == RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION.value


def test_pending_mail_remains_eligible_for_review_and_update(monkeypatch):
    record = _record(
        RecruitmentMailProcessingStatus.PENDING.value,
        category="rejection",
        application_id="app-pending",
    )
    store = _MailStore(record)
    repository = _Repository(
        SimpleNamespace(id="app-pending", stage=ApplicationStage.APPLIED)
    )
    review_calls = []
    update_calls = []

    def review(request, _store, _repository):
        review_calls.append(request.record_id)
        return SimpleNamespace(
            data=SimpleNamespace(
                association=SimpleNamespace(
                    match=SimpleNamespace(application_id="app-pending")
                ),
                approval_previews=[],
            )
        )

    def update(request, _repository, _store, **_kwargs):
        update_calls.append(
            (request.application_id, request.evidence_id, request.target_status)
        )
        return SimpleNamespace(success=True, data=SimpleNamespace(state="updated"))

    monkeypatch.setattr(scheduler_runtime, "review_recruitment_mail", review)
    monkeypatch.setattr(scheduler_runtime, "update_application_status", update)

    result = scheduler_runtime._link_unambiguous_recruitment_mail(store, repository)

    assert review_calls == [record.id]
    assert update_calls == [("app-pending", record.id, "rejected")]
    assert result["association_reviewed"] == 1
    assert result["association_linked"] == 1
    assert result["updated"] == 1
    assert result["unchanged"] == 0
    assert store.status_updates == [
        (record.id, RecruitmentMailProcessingStatus.PROCESSED_UPDATED, {})
    ]
