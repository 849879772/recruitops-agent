"""Mail client configuration contract, with no network transport."""

import pytest

from packages.recruitment_mail import processing
from tests.test_mail_model_processing import Client, setup_case


@pytest.mark.parametrize("api_style", ["openai", "anthropic"])
def test_mail_processor_forwards_api_style_to_shared_client(monkeypatch, api_style):
    store, repo, record, settings, triage, proposal = setup_case()
    settings.llm_api_key = "fixture-only-not-a-credential"
    settings.llm_model = "fixture-model"
    settings.llm_endpoint = "https://model.example.invalid/v1"
    settings.llm_timeout_seconds = 60
    settings.model_api_style = api_style
    constructed = []

    def client_factory(**kwargs):
        constructed.append(kwargs)
        return Client([triage, proposal])

    monkeypatch.setattr(processing, "DeepSeekClient", client_factory)
    result = processing.process_pending_mail(store, repo, settings)

    assert constructed == [{
        "api_key": settings.llm_api_key, "model": settings.llm_model,
        "endpoint": settings.llm_endpoint, "api_style": api_style,
        "max_tokens": 4000, "timeout": 25, "max_attempts": 1,
    }]
    assert result["unchanged"] == 1
    assert result["schedule_items_created"] == 1
    assert store.get(record.id).application_id == "4"
