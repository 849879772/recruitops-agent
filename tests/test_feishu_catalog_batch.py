from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import threading
import time

from scripts import repair_feishu_catalog_batch as runner
from packages.recruitment_core.jd_repair import content_sha256


def _raw_500() -> str:
    prefix = "岗位职责\n负责机器人数据采集、训练和部署，构建可持续迭代的数据闭"
    filler = " 参与跨团队协作并持续优化系统性能"
    return (prefix + filler * 30)[:500]


def _row(index: int, **changes) -> dict:
    row = {
        "id": f"job-{index}",
        "company_id": "company-1",
        "company_name": "示例公司",
        "title": f"机器人软件工程师-{index}",
        "city": "深圳",
        "detail_url": f"https://example.jobs.feishu.cn/398875/position/{1000 + index}/detail",
        "jd_raw": _raw_500(),
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "recruitment_campaign_id": "398875",
        "source_platform": "feishu",
        "source_tenant": "feishu:example.jobs.feishu.cn",
        "native_job_id": f"stored-{index}",
        "company_campus_url": "https://example.jobs.feishu.cn/398875/position/application",
        "analysis_status": "jd_incomplete",
        "analysis_match_score": None,
        "analysis_model": "",
    }
    row.update(changes)
    return row


def _hydration(row: dict, *, detail: str | None = None) -> dict:
    route_id = str(1000 + int(row["id"].split("-")[-1]))
    return {
        "detail": detail or ("岗位职责\n负责机器人系统开发和部署。\n任职要求\n熟悉 Python、C++、Linux。" * 20),
        "status": "complete",
        "source": "feishu_api",
        "detail_url": row["detail_url"],
        "identity_status": "matched",
        "identity_evidence": [f"native_id:{route_id}"],
    }


def test_query_scope_contains_strict_mutex_and_limit() -> None:
    captured = {}

    class Row:
        _mapping = {
            "id": "job-1",
            "scope_total": 1,
        }

    class Connection:
        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            captured["statement"] = statement
            return [Row()]

    rows, total = runner._query_rows(Connection(), max_rows=500)
    sql = captured["sql"].lower()
    assert "analysis_status" in sql and "jd_incomplete" in sql
    assert "a.match_score is null" in sql
    assert "a.model" in sql
    assert "cohort_status" in sql and "confirmed" in sql
    assert "source_platform" in sql and "feishu" in sql
    assert "count(*) over" in sql
    assert captured["params"]["max_rows"] == 500
    assert rows[0]["id"] == "job-1"
    assert total == 1


def test_request_guard_uses_explicit_proxy_and_timeout(monkeypatch) -> None:
    captured = {}

    def original_get(*args, **kwargs):
        captured.update(kwargs)
        return "response"

    monkeypatch.setattr(runner, "_ORIGINAL_REQUESTS_GET", original_get)
    token = runner._REQUEST_CONTEXT.set((3.0, "http://127.0.0.1:10808"))
    try:
        assert runner._bounded_direct_get(
            "https://example.jobs.feishu.cn",
            timeout=20,
        ) == "response"
    finally:
        runner._REQUEST_CONTEXT.reset(token)

    assert captured["timeout"] == 3.0
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:10808",
        "https": "http://127.0.0.1:10808",
    }


def test_non500_query_mode_and_explicit_exclusion_are_opt_in() -> None:
    captured = {}

    class Row:
        _mapping = {"id": "job-2", "scope_total": 1}

    class Connection:
        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            captured["statement"] = statement
            return [Row()]

    runner._query_rows(
        Connection(),
        max_rows=200,
        length_mode=runner.LENGTH_MODE_NON500,
        excluded_job_ids=["job-1"],
    )
    sql = captured["sql"].lower()
    assert "length(coalesce(j.jd_raw, '')) <> :stored_jd_chars" in sql
    assert "j.id not in (__[postcompile_excluded_job_ids])" in sql
    assert captured["params"]["max_rows"] == 200
    assert "excluded_job_ids" in captured["statement"]._bindparams


def test_successful_prior_report_ids_are_excluded_without_excluding_skips(tmp_path: Path) -> None:
    prior = {
        "schema": 1,
        "results": [
            {"job_id": "job-1", "selection": {"selected": True}, "validation": {"passed": True}},
            {"job_id": "job-2", "selection": {"selected": False}, "validation": {"passed": False}},
        ],
    }
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
    rows = [
        _row(1, jd_raw="短 JD"),
        _row(2, jd_raw="另一个短 JD"),
        _row(3, jd_raw="第三个短 JD"),
    ]
    calls = []

    def fetch(row, _timeout):
        calls.append(row["id"])
        return _hydration(row)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=rows,
        input_scope_total=3,
        fetcher=fetch,
        length_mode=runner.LENGTH_MODE_NON500,
        exclude_report_paths=[prior_path],
        max_rows=200,
        batch_timeout=30,
    )
    assert "job-1" not in calls
    assert set(calls) == {"job-2", "job-3"}
    assert report["summary"]["selected"] == 2


def test_skip_diagnosis_preserves_binding_rejection_without_fetch(tmp_path: Path) -> None:
    row = _row(1, detail_url="https://example.jobs.feishu.cn/398876/position/1001/detail")
    prior_item = {
        "job_id": row["id"],
        "company_id": row["company_id"],
        "company": row["company_name"],
        "title": row["title"],
        "failure_reason": "feishu_campaign_scope_mismatch",
        "selection": {"selected": False},
        "validation": {"passed": False},
    }
    prior_path = tmp_path / "wave03.json"
    prior_path.write_text(
        json.dumps({"schema": 1, "results": [prior_item]}, ensure_ascii=False),
        encoding="utf-8",
    )
    calls = []

    def fetch(row, _timeout):
        calls.append(row["id"])
        return _hydration(row)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=[_row(2, jd_raw="non500")],
        input_scope_total=1,
        diagnostic_rows=[row],
        fetcher=fetch,
        length_mode=runner.LENGTH_MODE_NON500,
        diagnose_report_paths=[prior_path],
        max_rows=200,
        batch_timeout=30,
    )
    diagnosis = report["diagnostics"]
    assert diagnosis["requested"] == 1
    assert diagnosis["found"] == 1
    assert diagnosis["binding_passed"] == 0
    item = diagnosis["records"][0]
    assert item["binding_diagnosis"]["status"] == "campaign_mismatch"
    assert item["binding_diagnosis"]["same_project_official_evidence"] is False
    assert calls == ["job-2"]


def test_summary_separates_detail_identity_validation_and_quality(tmp_path: Path) -> None:
    row = _row(1, title="机器人软件工程师")
    candidate = "岗位职责\n负责机器人系统开发。\n任职要求\n" + "熟悉 Python。" * 20

    def fetch(_row, _timeout):
        return _hydration(row, detail=candidate)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=[row],
        fetcher=fetch,
        batch_timeout=30,
    )
    summary = report["summary"]
    assert summary["detail_fetch_passed"] == 1
    assert summary["identity_passed"] == 1
    assert summary["validation_passed"] == 1
    assert summary["quality_assessed"] == 1
    assert summary["quality_complete"] in {0, 1}
    assert "quality_complete" in report["results"][0]


def test_selection_excludes_scored_model_and_non_feishu_rows(tmp_path: Path) -> None:
    rows = [
        _row(1),
        _row(2, analysis_match_score=88),
        _row(3, analysis_model="gpt-5.6-luna"),
        _row(4, source_platform="moka", source_tenant="moka:tenant"),
        _row(5, detail_url="https://example.jobs.feishu.cn/398875/position"),
    ]
    calls = []

    def fetch(row, _timeout):
        calls.append(row["id"])
        return _hydration(row)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=rows,
        fetcher=fetch,
        batch_timeout=30,
    )
    by_id = {item["job_id"]: item for item in report["results"]}
    assert calls == ["job-1"]
    assert by_id["job-1"]["validation"]["passed"] is True
    assert by_id["job-2"]["failure_reason"] == "already_scored"
    assert by_id["job-3"]["failure_reason"] == "model_semantic_exclusion"
    assert by_id["job-4"]["failure_reason"] == "source_not_explicit_feishu"
    assert by_id["job-5"]["failure_reason"] == "feishu_detail_route_missing"
    assert report["coverage"]["fully_covered"] is True
    assert report["summary"]["selected"] == 1


def test_three_smoke_then_expands_with_three_workers_and_keeps_failures(tmp_path: Path) -> None:
    rows = [_row(index) for index in range(1, 7)]
    calls: list[str] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fetch(row, _timeout):
        nonlocal active, max_active
        with lock:
            calls.append(row["id"])
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        if row["id"] == "job-5":
            raise RuntimeError("one row failed")
        return _hydration(row)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=rows,
        fetcher=fetch,
        batch_timeout=30,
    )
    assert set(calls[:3]) == {"job-1", "job-2", "job-3"}
    assert set(calls[3:]) == {"job-4", "job-5", "job-6"}
    assert max_active <= 3
    assert report["status"] == "complete"
    assert report["smoke_check"]["passed"] is True
    assert report["summary"]["selected"] == 6
    assert report["summary"]["passed"] == 5
    assert report["summary"]["failed"] == 1
    assert report["coverage"]["fully_covered"] is True
    failed = next(item for item in report["results"] if item["job_id"] == "job-5")
    assert "hydration_exception:RuntimeError" in failed["quality_rejection_reasons"]

    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["schema"] == 1
    assert saved["metadata"]["external_model_used"] is False
    assert saved["metadata"]["concurrency"] == 3
    assert saved["metadata"]["database_write"] is False


def test_original_project_binding_is_preserved_in_fetch_and_report(tmp_path: Path) -> None:
    row = _row(1)
    original_url = row["company_campus_url"]
    seen = []

    def fetch(job, _timeout):
        seen.append(job["company_campus_url"])
        return _hydration(job)

    report = runner.run_batch(
        output=tmp_path / "report.json",
        input_rows=[row],
        fetcher=fetch,
        batch_timeout=30,
    )
    item = report["results"][0]
    assert seen == [original_url]
    assert item["company_identity"]["company_campus_url"] == original_url
    assert item["project_identity"]["company_campus_url"] == original_url
    assert item["original_sha256"] == content_sha256(row["jd_raw"])
    assert item["candidate_sha256"] == content_sha256(item["candidate_jd"])


def test_pg_unavailable_is_checkpointed_as_blocked(tmp_path: Path) -> None:
    def unavailable():
        raise ConnectionError("database is down")

    output = tmp_path / "blocked.json"
    report = runner.run_batch(
        output=output,
        row_loader=unavailable,
        batch_timeout=30,
    )
    assert report["status"] == "blocked"
    assert report["error"]["failure_reason"] == "database_unavailable"
    assert report["coverage"]["fully_covered"] is False
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert saved["results"] == []


def test_resume_retries_inflight_and_reports_scope_overflow(tmp_path: Path) -> None:
    output = tmp_path / "resume.json"
    first = _row(1)
    second = _row(2)
    report = runner._new_report(
        runner._metadata(
            input_mode="offline",
            max_rows=2,
            concurrency=3,
            request_timeout=12,
            batch_timeout=30,
        )
    )
    report["results"] = []
    report["in_flight"] = [{"job_id": "job-1", "original_sha256": content_sha256(first["jd_raw"])}]
    output.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    calls = []

    def fetch(row, _timeout):
        calls.append(row["id"])
        return _hydration(row)

    resumed = runner.run_batch(
        output=output,
        resume=True,
        input_rows=[first, second],
        input_scope_total=3,
        fetcher=fetch,
        max_rows=2,
        batch_timeout=30,
    )
    assert set(calls) == {"job-1", "job-2"}
    assert resumed["status"] == "partial"
    assert resumed["coverage"]["actual_scope_total"] == 3
    assert resumed["coverage"]["fully_covered"] is False
    assert any(item["failure_reason"] == "scope_limit_reached" for item in resumed["not_run"])
