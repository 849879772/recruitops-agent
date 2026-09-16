from __future__ import annotations

import pytest

from evals.website_blind import (
    WebsiteBlindObservation,
    evaluate_website_blind,
    load_website_blind_fixture,
    run_website_blind_live,
    website_blind_manifest_stats,
)


def _perfect_observations():
    fixture = load_website_blind_fixture()
    return [
        WebsiteBlindObservation(
            case_id=case.case_id,
            connected=True,
            found_jobs=case.expected_jobs,
            found_detail_jobs=case.expected_detail_jobs,
            correct_titles=case.expected_jobs,
            complete_jd=case.expected_jobs,
            input_tokens=10,
            output_tokens=2,
        )
        for case in fixture.cases
    ]


def test_frozen_30_company_manifest_exposes_expected_statistics() -> None:
    stats = website_blind_manifest_stats()

    assert stats.cases == 30
    assert stats.expected_jobs == 2158
    assert stats.expected_detail_jobs == 2158
    assert len(stats.companies) == 30
    assert sum(stats.by_crawler.values()) == 30


def test_offline_blind_eval_reports_accuracy_tokens_and_failure_categories() -> None:
    summary = evaluate_website_blind(observations=_perfect_observations())

    assert summary.mode == "offline"
    assert summary.synthetic is True
    assert summary.integration_rate == 1.0
    assert summary.job_recall == 1.0
    assert summary.detail_recall == 1.0
    assert summary.accuracy == 1.0
    assert summary.total_tokens == 30 * 12
    assert summary.failure_categories == {}


def test_live_mode_is_explicit_and_runner_is_injectable() -> None:
    fixture = load_website_blind_fixture()

    def runner(case):
        return {
            "case_id": case.case_id,
            "connected": True,
            "found_jobs": case.expected_jobs,
            "found_detail_jobs": case.expected_detail_jobs,
            "correct_titles": case.expected_jobs,
            "complete_jd": case.expected_jobs,
        }

    summary = run_website_blind_live(fixture, runner)

    assert summary.mode == "live"
    assert summary.synthetic is False
    assert summary.job_recall == 1.0


def test_production_jd_coverage_excludes_non_scored_listing_rows() -> None:
    fixture = load_website_blind_fixture()
    observations = _perfect_observations()
    observations[0] = WebsiteBlindObservation(
        case_id=fixture.cases[0].case_id,
        connected=True,
        found_jobs=10,
        correct_titles=10,
        complete_jd=2,
        production_eligible_jobs=2,
        production_eligible_complete_jd=2,
        hydration_attempted=2,
        hydration_succeeded=2,
    )

    summary = evaluate_website_blind(fixture, observations)

    assert summary.production_evaluated_cases == 1
    assert summary.production_eligible_jobs == 2
    assert summary.production_eligible_complete_jd == 2
    assert summary.production_jd_coverage == 1.0
    assert "jd_incomplete" not in summary.failure_categories


def test_unknown_blind_observation_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown blind cases"):
        evaluate_website_blind(
            observations=[WebsiteBlindObservation(case_id="unknown-case", connected=True)]
        )
