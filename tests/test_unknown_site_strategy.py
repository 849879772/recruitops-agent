from __future__ import annotations

from evals.unknown_site_strategy import (
    StrategySpec,
    UnknownSiteCase,
    load_fixture,
    run_unknown_site_strategy_eval,
)


def test_unknown_site_fixture_freezes_moka_and_self_built_holdouts() -> None:
    fixture = load_fixture()

    assert len(fixture.cases) == 2
    assert {case.company for case in fixture.cases} == {"勇仕网络", "坤恒顺维"}
    assert all(len(case.strategies) == 3 for case in fixture.cases)


def test_unknown_site_strategy_report_rejects_partial_or_noisy_results() -> None:
    fixture = load_fixture()

    def runner(case: UnknownSiteCase, strategy: StrategySpec):
        expected = list(case.expected_titles)
        if strategy.name == "verified_manual_candidate":
            return expected, 12, None
        if strategy.name == "generic_extraction":
            return [expected[0], "产品测试平台"], 8, None
        return [], 1, None

    report = run_unknown_site_strategy_eval(fixture=fixture, probe_runner=runner)

    assert report.synthetic is False
    assert [summary.safe_connections for summary in report.summaries] == [0, 0, 2]
    assert all(
        row.safe_to_connect
        for row in report.observations
        if row.strategy == "verified_manual_candidate"
    )
    assert all(
        not row.safe_to_connect
        for row in report.observations
        if row.strategy != "verified_manual_candidate"
    )
