import pytest

from evals.metrics import WebsiteEvalResult, summarize_websites


def test_website_eval_summary_uses_explicit_denominators() -> None:
    summary = summarize_websites(
        [
            WebsiteEvalResult(
                company="A",
                connected=True,
                expected_jobs=10,
                found_jobs=9,
                correct_titles=9,
                complete_jd=8,
                steps=4,
                token_cost=0.2,
            ),
            WebsiteEvalResult(
                company="B",
                connected=False,
                expected_jobs=5,
                found_jobs=3,
                correct_titles=2,
                complete_jd=1,
                manual_interventions=1,
                steps=8,
                token_cost=0.4,
            ),
        ]
    )

    assert summary.cases == 2
    assert summary.integration_rate == 0.5
    assert summary.job_recall == 12 / 15
    assert summary.title_accuracy == 11 / 12
    assert summary.jd_coverage == 9 / 12
    assert summary.manual_intervention_rate == 0.5
    assert summary.mean_steps == 6
    assert summary.mean_token_cost == pytest.approx(0.3)


def test_overfetch_on_one_company_does_not_hide_missed_jobs_on_another() -> None:
    summary = summarize_websites(
        [
            WebsiteEvalResult(
                company="A",
                connected=True,
                expected_jobs=5,
                found_jobs=10,
                correct_titles=10,
                complete_jd=10,
            ),
            WebsiteEvalResult(
                company="B",
                connected=False,
                expected_jobs=5,
                found_jobs=0,
                correct_titles=0,
                complete_jd=0,
            ),
        ]
    )

    assert summary.job_recall == 0.5
