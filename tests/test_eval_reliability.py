from __future__ import annotations

import json

from evals.fake_model import FakeModel
from evals.reliability import load_reliability_fixture, run_reliability_eval


def test_no_key_golden_reliability_eval_covers_all_requested_surfaces() -> None:
    report = run_reliability_eval()

    assert report.mode == "offline"
    assert report.synthetic is True
    assert report.cases == 12
    assert report.passed_cases == report.cases
    assert report.accuracy == 1.0
    assert report.total_tokens == report.input_tokens + report.output_tokens
    assert report.total_cost_usd == 0.0
    assert {item.area for item in report.results} == {
        "intent",
        "plan",
        "mail_classification",
        "job_matching",
    }
    assert report.failure_categories == {"model_output_invalid": 1}


def test_fake_model_is_case_injectable_and_records_usage_without_a_key() -> None:
    model = FakeModel(
        {
            "case-1": {
                "output": {"label": "interview"},
                "input_tokens": 7,
                "output_tokens": 3,
                "cost_usd": 0.02,
            }
        }
    )

    response = model.bind("case-1", operation="mail").complete(
        system_prompt="system",
        user_prompt="user",
    )
    call = model.last_call("case-1")

    assert response.content == '{"label": "interview"}'
    assert call is not None
    assert call.total_tokens == 10
    assert call.cost_usd == 0.02
    assert not hasattr(model, "api_key")


def test_fixture_rejects_personal_data(tmp_path) -> None:
    fixture = load_reliability_fixture()
    payload = fixture.model_dump(mode="json")
    payload["contains_personal_data"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        load_reliability_fixture(path)
    except ValueError as exc:
        assert "personal data" in str(exc)
    else:
        raise AssertionError("personal-data fixture was accepted")
