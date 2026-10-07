"""Channel classification must not change stages or invent a progress page."""
import pytest

from packages.domain.models import Application, ApplicationStage
from packages.domain.urls import application_progress_channel, normalize_http_page_url


@pytest.mark.parametrize("record_url", [
    None, "", "   ", "没有官网进度页", "mailto:recruitment@example.test",
    "file:///C:/applications.html", "//example.test/progress", "https://",
    "https://example.test:bad/progress", "https://user:secret@example.test/progress",
    "https://bad host/progress", "https:///careers.example.test",
])
def test_missing_or_invalid_saved_progress_link_is_mail_only(record_url):
    assert application_progress_channel(record_url) == "mail_only"


@pytest.mark.parametrize("record_url", [
    "https://example.test/progress", "http://example.test/progress",
    " https://example.test/#/application ", "https://example.test:8443/application",
])
def test_valid_saved_progress_link_allows_official_page_review(record_url):
    assert application_progress_channel(record_url) == "official_page"


def test_review_channel_and_page_evidence_share_whitespace_normalization():
    assert normalize_http_page_url(" https://example.test/progress ") == "https://example.test/progress"
    assert normalize_http_page_url(" https://example.test/#/application ") == "https://example.test/#/application"


def test_adding_and_removing_a_link_changes_only_the_derived_channel():
    original = Application(
        id="mail-only", company_name="示例公司", job_title="工程师",
        job_id="job-with-its-own-link", record_url=None, stage=ApplicationStage.INTERVIEW1,
        idempotency_key="mail-only", source="manual",
        stage_history=[{"stage": "applied"}, {"stage": "interview1"}],
    )
    linked = original.model_copy(update={"record_url": "https://example.test/application"})
    unlinked = linked.model_copy(update={"record_url": None})
    assert application_progress_channel(original.record_url) == "mail_only"
    assert application_progress_channel(linked.record_url) == "official_page"
    assert application_progress_channel(unlinked.record_url) == "mail_only"
    assert original.stage == linked.stage == unlinked.stage == ApplicationStage.INTERVIEW1
    assert original.stage_history == linked.stage_history == unlinked.stage_history
