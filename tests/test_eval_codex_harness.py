import pytest

from evals.codex_harness import (
    CodexHarnessResult,
    compare_golden_results,
)


def test_error_result_is_never_counted_as_success() -> None:
    result = CodexHarnessResult(
        case_id="case-error",
        tool_name="search_jobs",
        success=True,
        error_code="provider_timeout",
    )

    assert result.success is False
    assert CodexHarnessResult(case_id="status-error", status="error").success is False


def test_golden_report_aggregates_tool_success_latency_and_tokens() -> None:
    old = [
        {
            "case_id": "case-1",
            "tool_name": "search_jobs",
            "success": True,
            "latency_ms": 100,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
        {
            "case_id": "case-2",
            "tool_name": "company_coverage",
            "success": False,
            "latency_ms": 300,
            "input_tokens": 20,
            "output_tokens": 5,
        },
    ]
    new = [
        {
            "case_id": "case-1",
            "selected_tool": "search_jobs",
            "success": True,
            "latency_ms": 80,
            "input_tokens": 8,
            "output_tokens": 3,
        },
        {
            "case_id": "case-2",
            "tool": "crawler_acceptance",
            "success": True,
            "latency_ms": 200,
            "usage": {"total_tokens": 18},
        },
    ]

    report = compare_golden_results(old, new)

    assert report.summary.cases == 2
    assert report.summary.tool_selection_matches == 1
    assert report.summary.tool_changes == 1
    assert report.summary.old_success_rate == 0.5
    assert report.summary.new_success_rate == 1.0
    assert report.summary.success_rate_delta == 0.5
    assert report.summary.old_mean_latency_ms == 200.0
    assert report.summary.new_mean_latency_ms == 140.0
    assert report.summary.latency_delta_ms == -60.0
    assert report.summary.old_total_tokens == 39
    assert report.summary.new_total_tokens == 29
    assert report.summary.token_delta == -10
    assert report.model_dump()["cases"][1]["new_tool"] == "crawler_acceptance"


def test_duplicate_and_unknown_case_shapes_are_reported_or_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="duplicate Codex Harness case"):
        compare_golden_results(
            [{"case_id": "case-1"}, {"case_id": "case-1"}],
            [],
        )

    report = compare_golden_results(
        [{"case_id": "old-only", "success": True}],
        [{"case_id": "new-only", "success": True}],
    )
    output = report.write_json(tmp_path / "golden-report.json")

    assert output.is_file()
    assert report.missing_from_old == ["new-only"]
    assert report.missing_from_new == ["old-only"]
    assert '"evaluation": "codex_harness_golden"' in output.read_text(encoding="utf-8")
