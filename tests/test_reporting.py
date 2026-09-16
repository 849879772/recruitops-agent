from __future__ import annotations

import json

from packages.domain.models import Company, JobPage
from packages.reporting import (
    build_reporting_summary,
    crawler_health_reconciliation,
    daily_pipeline_summary,
    report_to_json,
)


def _company_row(company_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "company_id": company_id,
        "company_name": company_id.title(),
        "status": "completed",
        "raw_job_count": 10,
        "accepted_job_count": 10,
    }
    row.update(overrides)
    return row


def test_daily_summary_is_json_safe_and_deterministically_sorted() -> None:
    result = {
        "dry_run": False,
        "total_companies": 2,
        "selected_companies": 2,
        "crawled_companies": 2,
        "new": 2,
        "changed": 1,
        "reused": 3,
        "rejected": 1,
        "failed": 0,
        "written": True,
        "failure_reasons": {"crawler_failed": 0},
        "rejection_reasons": {"invalid_job": 1},
        "companies": [
            _company_row("company-b"),
            _company_row(
                "company-a",
                new_count=2,
                filtered_reasons={"internship": 1, "direction_out": 2},
            ),
        ],
    }

    summary = daily_pipeline_summary(result)

    assert summary["counts"]["new"] == 2
    assert [item["company_id"] for item in summary["companies"]] == [
        "company-a",
        "company-b",
    ]
    assert summary["companies"][0]["filtered_reasons"] == {
        "direction_out": 2,
        "internship": 1,
    }
    encoded = report_to_json(summary)
    assert json.loads(encoded) == summary
    assert "legacy" not in encoded


def test_health_reconciliation_covers_required_issue_types() -> None:
    current = {
        "companies": [
            _company_row(
                "failed",
                status="failed",
                raw_job_count=0,
                failure_reason="crawler_failed",
            ),
            _company_row("empty", raw_job_count=0, accepted_job_count=0),
            _company_row(
                "partial",
                raw_job_count=4,
                pages_seen=1,
                total_pages=2,
                has_more=True,
                failure_reason="pagination_incomplete",
            ),
            _company_row("changed", raw_job_count=15),
            _company_row("stable", raw_job_count=10),
        ]
    }
    previous = {
        "companies": [
            _company_row("changed", raw_job_count=10),
            _company_row("stable", raw_job_count=10),
        ]
    }

    report = crawler_health_reconciliation(current, previous)

    assert report["status"] == "degraded"
    assert report["issue_counts"] == {
        "crawler_failed": 1,
        "zero_results": 1,
        "pagination_incomplete": 1,
        "quantity_change": 1,
    }
    assert report["baseline_source"] == "previous_pipeline_result"
    changed = next(item for item in report["issues"] if item["company_id"] == "changed")
    assert changed["details"]["relative_change"] == 0.5
    assert changed["details"]["direction"] == "increase"


class _Repository:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def list_companies(self) -> list[Company]:
        return [
            Company(
                id="empty",
                name="Empty",
                integration_status="connected",
                source="test",
            ),
            Company(
                id="pending",
                name="Pending",
                integration_status="pending",
                source="test",
            ),
        ]

    def search_jobs(self, **kwargs: object) -> JobPage:
        company = str(kwargs["company"])
        self.queries.append(company)
        return JobPage(items=[], total=0, limit=1, offset=0)


def test_health_falls_back_to_agent_postgres_snapshot_without_writes() -> None:
    repository = _Repository()

    report = crawler_health_reconciliation(repository=repository)

    assert report["observation_source"] == "agent_postgres"
    assert report["issue_counts"]["zero_results"] == 1
    assert report["affected_company_ids"] == ["empty"]
    assert repository.queries == ["empty", "pending"]


def test_health_uses_one_aggregate_company_count_query_when_available() -> None:
    class AggregateRepository(_Repository):
        aggregate_calls = 0

        def job_counts_by_company(self) -> dict[str, int]:
            self.aggregate_calls += 1
            return {"empty": 2}

    repository = AggregateRepository()
    report = crawler_health_reconciliation(repository=repository)

    assert report["issue_counts"]["zero_results"] == 0
    assert repository.aggregate_calls == 1
    assert repository.queries == []


def test_combined_report_marks_reporting_safety_boundary() -> None:
    report = build_reporting_summary(
        {"companies": [_company_row("company-a")]},
        run_id="run-1",
    )

    assert report["report_type"] == "daily_pipeline_and_crawler_health"
    assert report["run"]["run_id"] == "run-1"
    assert report["safety"] == {
        "reporting_write_attempted": False,
        "agent_postgres_read_attempted": False,
        "agent_postgres_write_attempted": False,
        "legacy_system_read_attempted": False,
        "legacy_system_write_attempted": False,
        "external_web_access_attempted": False,
        "model_call_attempted": False,
        "source_write_attempted": False,
    }
