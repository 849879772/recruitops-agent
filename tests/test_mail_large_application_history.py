from types import SimpleNamespace
import pytest
from tests.test_mail_model_processing import setup_case, Client
from packages.recruitment_mail.processing import process_pending_mail, _failure_diagnostic


@pytest.mark.parametrize("count", [50, 51, 500])
def test_mail_analysis_does_not_fail_or_miss_target_after_first_fifty(count):
    store, repo, record, settings, triage, proposal = setup_case()
    target = repo.application
    applications = [target.model_copy(update={"id": f"other-{i}", "company_name": f"Other company {i}", "job_title": "Other role"})
                    for i in range(count)] + [target]
    repo.list_applications = lambda: applications
    proposal["candidate_application_id"] = None
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["failed"] == 0 and result["unchanged"] == 1
    assert store.get(record.id).application_id == target.id
    assert client.calls == 2


def test_known_bound_failure_is_diagnostic_not_generic_valueerror():
    assert _failure_diagnostic(ValueError("mail_body_exceeds_bounds"))["code"] == "mail_body_exceeds_bounds"
    assert "private" not in str(_failure_diagnostic(ValueError("private mail text")))
