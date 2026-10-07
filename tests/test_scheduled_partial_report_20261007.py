from types import SimpleNamespace

import pytest

from packages.automation.latest_report import summarize


@pytest.mark.parametrize("payload, expected", [
    ({"status": "completed", "sync_status": "succeeded"}, "succeeded"),
    ({"status": "partial", "sync_status": "degraded"}, "partial"),
    ({"status": "degraded"}, "partial"),
    ({"status": "completed", "sync_status": "degraded"}, "partial"),
    ({"status": "completed", "daily_sync": {"status": "degraded"}}, "partial"),
])
def test_completed_and_partial_reports_do_not_invent_failure(payload, expected):
    report = summarize(payload, "scheduled-run")
    assert report["status"] == expected
    assert report["error"] is None
    assert report["source_status"] == payload["status"]


def test_partial_report_preserves_committed_work_and_final_scoring_totals():
    payload = {
        "status": "partial", "sync_status": "degraded",
        "new": 149, "reused": 18198, "failed_jobs": 1281,
        "scoring_candidates": 2817, "scored": 2773, "scoring_failed": 44, "unscored": 44,
        "failure_reasons": {"timeout": 5, "model_output_invalid": 44},
        "write_statistics": {"job_snapshot_update_count": 18198},
        "warnings": ["部分岗位评分失败"],
        "daily_sync": {
            "status": "degraded", "warnings": ["部分公司抓取失败", "部分岗位评分失败"],
            "pipeline": {
                "selected_companies": 1395, "new": 149, "reused": 18198,
                "scoring_candidates": 0, "scored": 0, "scoring_failed": 0, "unscored": 0,
                "failure_reasons": {"timeout": 5},
                "companies": [
                    {"status": "complete", "detail_success_count": 2},
                    {"status": "complete", "detail_failure_count": 1},
                    {"status": "failed"},
                ],
            },
        },
    }
    report = summarize(payload, "scheduled-run")
    assert report["status"] == "partial" and report["error"] is None
    assert (report["new_jobs"], report["updated_jobs"], report["reused_jobs"]) == (149, 18198, 18198)
    assert (report["scoring_candidates"], report["scored_jobs"], report["scoring_failed"],
            report["unscored_jobs"]) == (2817, 2773, 44, 44)
    assert (report["complete_companies"], report["partial_companies"], report["failed_companies"]) == (1, 1, 1)
    assert (report["detail_success"], report["detail_failed"], report["failed_jobs"]) == (2, 1, 1281)
    assert report["warnings"] == ["部分公司抓取失败", "部分岗位评分失败"]
    assert report["failure_reasons"] == {"timeout": 5, "model_output_invalid": 44}


def test_report_falls_back_to_pipeline_metrics_without_runtime_overrides():
    report = summarize({"status": "completed", "daily_sync": {"pipeline": {
        "new": 3, "scoring_candidates": 5, "scored": 5,
        "job_write_statistics": {"updated_count": 2},
    }}}, "scheduled-run")
    assert report["status"] == "succeeded"
    assert (report["new_jobs"], report["updated_jobs"], report["scoring_candidates"],
            report["scored_jobs"]) == (3, 2, 5, 5)


@pytest.mark.parametrize("payload", [
    {"status": "failed", "error": "crawl failed"},
    {"status": "blocked", "error": "write_disabled"},
    {"status": "configuration_required", "missing": ["title_keywords"]},
    {"status": "completed", "daily_sync": {"status": "failed", "error": "fatal crawl error"}},
    {"status": "partial", "sync_status": "failed", "error": "fatal scoring error"},
    {"status": "partial", "daily_sync": {"pipeline": {"status": "failed"}}},
])
def test_explicit_fatal_status_still_reports_failure(payload):
    report = summarize(payload, "scheduled-run")
    assert report["status"] == "failed"
    assert report["error"]


def test_nested_fatal_pipeline_keeps_its_reason_and_actual_status():
    payload = {"status": "partial", "daily_sync": {"pipeline": {
        "status": "failed", "error": "storage transaction failed",
    }}}
    assert summarize(payload, "scheduled-run")["error"] == "storage transaction failed"
    del payload["daily_sync"]["pipeline"]["error"]
    assert summarize(payload, "scheduled-run")["error"] == "全量任务执行失败（状态：failed）"


def test_startup_failure_without_pipeline_preserves_original_diagnostic():
    diagnostic = "JsonRpcRemoteError: required MCP recruitops timed out handshaking after 20s"
    report = summarize({"status": "failed", "error": diagnostic}, "scheduled-run")
    assert report["status"] == "failed"
    assert report["error"] == diagnostic
    assert report["new_jobs"] is None and report["scored_jobs"] is None


def test_nonfatal_diagnostic_remains_visible_on_degraded_completion():
    diagnostic = "discovery failed; using configured companies"
    report = summarize({
        "status": "completed", "sync_status": "degraded",
        "daily_sync": {"error": diagnostic},
    }, "scheduled-run")
    assert report["status"] == "partial"
    assert report["error"] == diagnostic


@pytest.mark.parametrize("status", ["completed", "partial", "failed"])
def test_report_redacts_warning_message_and_error_diagnostics(status):
    settings = SimpleNamespace(
        llm_api_key="fixture-provider-credential",
        mail_imap_password="fixture-mail-passphrase",
        database_url="postgresql://fixture:fixture-db-pass@db.example.test/recruitment",
    )
    secrets = [settings.llm_api_key, settings.mail_imap_password, settings.database_url,
               "sk-0123456789abcdef01234567", "fixture@example.test"]
    diagnostic = "provider unavailable: " + "; ".join(secrets)
    report = summarize({
        "status": status, "error": diagnostic, "message": diagnostic,
        "warnings": [diagnostic],
    }, "scheduled-run", settings=settings)
    for value in (report["error"], report["message"], report["warnings"][0]):
        assert "provider unavailable" in value
        assert "[REDACTED:" in value
        assert all(secret not in value for secret in [*secrets, "fixture-db-pass"])
