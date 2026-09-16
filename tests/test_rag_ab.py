import json
from pathlib import Path

from evals.rag_ab import (
    DEFAULT_FIXTURE_PATH,
    OnboardingExecution,
    estimate_tokens,
    load_rag_onboarding_fixture,
    main,
    run_frozen_rag_ab,
    run_rag_ab,
)


def test_fixture_has_independent_ground_truth_for_each_paired_case() -> None:
    fixture = load_rag_onboarding_fixture()

    assert fixture.source_type == "fixture"
    assert fixture.result_type == "synthetic"
    assert len(fixture.cases) == 6
    assert len({case.case_id for case in fixture.cases}) == 6
    assert all(case.ground_truth.jobs for case in fixture.cases)
    assert all(set(case.synthetic_runs) == {"baseline", "rag"} for case in fixture.cases)
    assert all(case.source_url.startswith("https://example.invalid/") for case in fixture.cases)


def test_frozen_ab_report_is_deterministic_and_reports_both_strategies() -> None:
    first = run_frozen_rag_ab()
    second = run_frozen_rag_ab()

    assert first == second
    assert first.cases == 6
    assert first.baseline.successful_cases == 2
    assert first.baseline.success_rate == 2 / 6
    assert first.rag.successful_cases == 6
    assert first.rag.success_rate == 1.0
    assert first.baseline.mean_steps == 50 / 6
    assert first.rag.mean_steps == 34 / 6
    assert first.baseline.manual_intervention_cases == 4
    assert first.rag.manual_intervention_cases == 0
    assert first.baseline.estimated_input_tokens == 1810
    assert first.rag.estimated_output_tokens == 445
    assert first.source_type == "fixture"
    assert first.result_type == "synthetic"
    assert first.synthetic is True


def test_injected_executor_runs_each_case_in_both_modes_and_estimates_tokens() -> None:
    case = load_rag_onboarding_fixture().cases[0]
    calls: list[tuple[str, str]] = []

    def executor(case, strategy):
        calls.append((case.case_id, strategy))
        return OnboardingExecution(
            status=case.ground_truth.status,
            jobs=case.ground_truth.jobs,
            steps=2,
            input_text="abcd",
            output_text="12345678",
        )

    report = run_rag_ab(
        [case],
        executor=executor,
        fixture_id="injected-test-fixture",
        frozen_at=case.frozen_at,
    )

    assert calls == [(case.case_id, "baseline"), (case.case_id, "rag")]
    assert report.baseline.success_rate == 1.0
    assert report.rag.success_rate == 1.0
    assert report.baseline.estimated_input_tokens == 1
    assert report.rag.estimated_output_tokens == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("12345678") == 2


def test_success_is_checked_against_ground_truth_not_executor_claims() -> None:
    case = load_rag_onboarding_fixture().cases[0]

    def dishonest_executor(case, strategy):
        return {
            "status": "connected",
            "jobs": [],
            "steps": 1,
            "manual_interventions": 0,
            "estimated_input_tokens": 1,
            "estimated_output_tokens": 1,
        }

    report = run_rag_ab(
        [case],
        executor=dishonest_executor,
        fixture_id="ground-truth-test",
        frozen_at=case.frozen_at,
    )

    assert report.baseline.successful_cases == 0
    assert report.rag.successful_cases == 0
    assert report.baseline.results[0].mismatch_reasons == ["jobs_mismatch"]


def test_cli_prints_and_can_write_a_json_report(tmp_path: Path, capsys) -> None:
    output_path = tmp_path / "nested" / "rag-ab.json"

    assert main(["--fixture", str(DEFAULT_FIXTURE_PATH), "--output", str(output_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert printed == written
    assert printed["evaluation"] == "website_onboarding_rag_ab"
    assert printed["source_type"] == "fixture"
    assert printed["result_type"] == "synthetic"
    assert printed["baseline"]["cases"] == printed["rag"]["cases"] == 6
