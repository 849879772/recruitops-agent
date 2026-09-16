from scripts.run_oc_full_crawl_eval import (
    _drop_retry_statuses,
    _load_checkpoint,
    _summary,
    build_parser,
)


def test_full_oc_summary_keeps_acceptance_categories_and_job_counts() -> None:
    result = _summary([
        {
            "integration_status": "connected_complete",
            "raw_job_count": 5,
            "accepted_count": 4,
            "rejected_count": 1,
        },
        {
            "integration_status": "needs_adapter",
            "raw_job_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "error_code": "timeout",
        },
    ])

    assert result == {
        "company_count": 2,
        "source_project_count": 2,
        "deduplicated_entry_count": 0,
        "integration_statuses": {"connected_complete": 1, "needs_adapter": 1},
        "raw_job_count": 5,
        "accepted_job_count": 4,
        "rejected_job_count": 1,
        "complete_jd_count": 0,
        "incomplete_jd_count": 0,
        "job_evidence_count": 0,
        "unique_job_evidence_count": 0,
        "duplicate_job_evidence_count": 0,
        "error_codes": {"timeout": 1},
    }


def test_full_oc_checkpoint_uses_latest_row_for_same_lead(tmp_path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    path.write_text(
        '{"lead_key":"a","raw_job_count":1}\n'
        '{"lead_key":"a","raw_job_count":2}\n',
        encoding="utf-8",
    )

    assert _load_checkpoint(path)["a"]["raw_job_count"] == 2


def test_retry_status_removes_only_requested_checkpoint_rows() -> None:
    rows = {
        "a": {"integration_status": "connected_complete"},
        "b": {"integration_status": "connected_partial"},
        "c": {"integration_status": "needs_adapter"},
    }

    kept = _drop_retry_statuses(rows, {"connected_partial", "needs_adapter"})

    assert kept == {"a": {"integration_status": "connected_complete"}}


def test_retry_status_can_be_limited_to_prior_crawler() -> None:
    rows = {
        "a": {"integration_status": "connected_partial", "crawler_key": "moka"},
        "b": {"integration_status": "connected_partial", "crawler_key": "render"},
        "c": {"integration_status": "needs_adapter", "crawler_key": "feishu"},
    }

    kept = _drop_retry_statuses(
        rows,
        {"connected_partial", "needs_adapter"},
        {"moka", "feishu"},
    )

    assert kept == {
        "b": {"integration_status": "connected_partial", "crawler_key": "render"}
    }


def test_full_oc_cli_accepts_detail_hydration_status() -> None:
    args = build_parser().parse_args([
        "--hydrate-details",
        "--retry-status",
        "jd_hydration_required",
    ])

    assert args.hydrate_details is True
    assert args.retry_status == ["jd_hydration_required"]
