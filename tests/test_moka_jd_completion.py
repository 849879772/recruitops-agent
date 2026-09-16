from __future__ import annotations

import json
from pathlib import Path
import time

from packages.recruitment_core import job_details
from scripts import repair_moka_catalog_batch as batch


def _row(company: str = "九号公司", index: int = 0) -> dict:
    job_id = f"job-{company}-{index}"
    return {
        "id": job_id,
        "company_id": f"company-{company}",
        "company_name": company,
        "title": f"工程师-{index}",
        "city": "北京市",
        "detail_url": f"https://app.mokahr.com/campus_apply/demo/54046#/job/{index:032d}",
        "company_campus_url": "https://app.mokahr.com/campus_apply/demo/54046",
        "jd_raw": "工程师",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "2027",
        "source_platform": "moka",
        "source_tenant": "moka:demo",
        "native_job_id": job_id,
        "recruitment_campaign_id": None,
        "company_crawler_key": "moka",
        "model": None,
        "analysis_status": "jd_incomplete",
    }


def _render_row() -> dict:
    row = _row()
    row.update(source_platform="render", source_tenant="moka:demo")
    return row


def test_moka_detail_content_uses_only_explicit_text_fields() -> None:
    detail, fields = job_details._moka_detail_content(
        {
            "jobDescription": "<p>岗位职责</p><p>负责软件开发和测试。</p>",
            "jobRequirements": "<p>本科及以上，熟悉 Python。</p>",
            "customFields": {"secret_body": "不应进入 JD"},
            "jobIntentions": [{"description": "元数据，不是正文"}],
        }
    )

    assert fields == ("jobDescription", "jobRequirements")
    assert "岗位职责" in detail and "任职要求" in detail
    assert "不应进入 JD" not in detail
    assert "元数据，不是正文" not in detail


def test_moka_detail_content_does_not_stringify_unknown_json() -> None:
    detail, fields = job_details._moka_detail_content(
        {
            "jobDescription": "岗位职责\n负责平台维护。",
            "requirements": {"text": "本科及以上"},
            "candidateRequirements": "本科及以上",
        }
    )

    assert fields == ("jobDescription",)
    assert "candidateRequirements" not in detail
    assert '"text"' not in detail


def test_moka_detail_content_deduplicates_embedded_requirements() -> None:
    detail, fields = job_details._moka_detail_content(
        {
            "jobDescription": "岗位职责\n负责平台维护。\n任职要求\n本科及以上。",
            "requirements": "本科及以上。",
        }
    )

    assert fields == ("jobDescription",)
    assert detail.count("任职要求") == 1


def test_baseline_selection_is_exactly_two_per_company() -> None:
    rows = [
        _row(company, index)
        for company in batch.BASELINE_COMPANIES
        for index in range(3)
    ]

    selected, missing = batch._select_baseline(rows)

    assert missing == []
    assert len(selected) == 12
    assert {
        company: sum(row["company_name"] == company for row in selected)
        for company in batch.BASELINE_COMPANIES
    } == {company: 2 for company in batch.BASELINE_COMPANIES}


def test_exclude_report_skips_success_failure_and_skipped_job_ids(tmp_path) -> None:
    report_path = tmp_path / "wave01-report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "results": [
                    {"job_id": "success-1"},
                    {"job_id": "failed-1", "failure_reason": "timeout"},
                    {"job_id": "skipped-1", "selection": {"selected": False}},
                ],
                "path_confirmation": {"status": "confirmed", "sample_size": 12},
                "read_only": True,
                "model_calls": 0,
                "database_writes": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "status": "complete",
                "completed_job_ids": ["success-1", "failed-1", "skipped-1"],
                "in_flight_job_ids": [],
            }
        ),
        encoding="utf-8",
    )

    excluded, reports = batch._load_exclude_context((report_path.resolve(),))
    proof = batch._confirmed_path_proof(reports)

    assert excluded == {"success-1", "failed-1", "skipped-1"}
    assert reports[0][0] == report_path.resolve()
    assert proof["status"] == "confirmed"
    assert proof["sample_size"] == 12
    assert proof["new_samples"] == 0


def test_confirmed_prior_proof_skips_consumed_baseline() -> None:
    rows = [_row("未在首轮出现", index) for index in range(3)]
    proof = {
        "status": "confirmed",
        "source": "prior_report",
        "source_reports": ["wave01/report.json"],
        "sample_size": 12,
        "companies": list(batch.BASELINE_COMPANIES),
        "failures": [],
    }

    selected, missing = batch._select_targets(
        rows,
        limit=2,
        sample_only=False,
        path_proof=proof,
    )

    assert missing == []
    assert [row["id"] for row in selected] == ["job-未在首轮出现-0", "job-未在首轮出现-1"]


def test_parser_accepts_repeatable_exclude_report() -> None:
    args = batch._parser().parse_args(
        [
            "--exclude-report",
            "wave01/report.json",
            "--exclude-report",
            "older/report.json",
            "--include-render",
        ]
    )

    assert args.exclude_report == [Path("wave01/report.json"), Path("older/report.json")]
    assert args.include_render is True


def test_trusted_render_moka_row_is_admitted_without_changing_source() -> None:
    row = _render_row()

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is True
    assert evidence["status"] == "accepted"
    assert evidence["job_id"]
    assert row["source_platform"] == "render"


def test_render_moka_accepts_same_host_root_company_entry() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://campus.allwinnertech.com/campus-recruitment/"
            "allwinnertech/43436#/job/1c41bb77-5277-43fb-ac36-382db590a0c1"
        ),
        company_campus_url="https://campus.allwinnertech.com/",
        source_tenant="web:campus.allwinnertech.com:/",
    )

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is True
    assert evidence["provenance_mode"] == "same_host_detail_route"


def test_render_moka_accepts_bound_shared_host_tenant_company_url() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://app.mokahr.com/campus_apply/demo/54046"
            "#/job/1c41bb77-5277-43fb-ac36-382db590a0c1"
        ),
        company_campus_url="https://app.mokahr.com/campus_apply/demo/54046",
        source_tenant="moka:demo",
    )

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is True
    assert evidence["tenant_evidence"] == "render_moka_tenant_matches_site_path"


def test_render_moka_rejects_shared_host_root_without_company_binding() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://app.mokahr.com/campus_apply/demo/54046"
            "#/job/1c41bb77-5277-43fb-ac36-382db590a0c1"
        ),
        company_campus_url="https://app.mokahr.com/",
        source_tenant="web:app.mokahr.com:/",
    )

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is False
    assert evidence["reason"] == "render_moka_shared_host_root_unbound"


def test_render_moka_rejects_shared_host_cross_tenant_company_url() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://app.mokahr.com/campus_apply/demo/54046"
            "#/job/1c41bb77-5277-43fb-ac36-382db590a0c1"
        ),
        company_campus_url="https://app.mokahr.com/campus_apply/other-tenant/54046",
        source_tenant="moka:demo",
    )

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is False
    assert evidence["reason"] == "render_moka_company_site_mismatch"


def test_render_moka_rejects_cross_host_company_entry() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://jobs.example.test/campus-recruitment/demo/54046"
            "#/job/1c41bb77-5277-43fb-ac36-382db590a0c1"
        ),
        company_campus_url="https://other.example.test/campus-recruitment/demo/54046",
    )

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is False
    assert evidence["reason"] == "render_moka_cross_host_company_entry"


def test_render_moka_rejects_list_only_and_missing_job_id() -> None:
    list_row = _render_row()
    list_row["detail_url"] = "https://app.mokahr.com/campus_apply/demo/54046#/jobs"
    no_id_row = _render_row()
    no_id_row["detail_url"] = "https://app.mokahr.com/campus_apply/demo/54046"

    list_accepted, list_evidence = batch._render_moka_admission(list_row)
    no_id_accepted, no_id_evidence = batch._render_moka_admission(no_id_row)

    assert list_accepted is False
    assert no_id_accepted is False
    assert list_evidence["reason"] == "render_moka_exact_job_coordinates_missing"
    assert no_id_evidence["reason"] == "render_moka_exact_job_coordinates_missing"


def test_render_moka_rejects_fake_tenant() -> None:
    row = _render_row()
    row["source_tenant"] = "moka:not-demo"

    accepted, evidence = batch._render_moka_admission(row)

    assert accepted is False
    assert evidence["reason"] == "render_moka_tenant_site_mismatch"


def test_render_rejection_evidence_drops_query_parameters() -> None:
    row = _render_row()
    row.update(
        detail_url=(
            "https://other.example.test/campus-recruitment/demo/54046"
            "#/job/1c41bb77-5277-43fb-ac36-382db590a0c1?access_token=secret"
        ),
        company_campus_url="https://jobs.example.test/campus-recruitment/demo/54046",
        source_tenant="web:other.example.test:/jobs?token=secret",
    )

    _, rejected = batch._admit_render_moka_rows([row])

    assert rejected[0]["detail_url"] == "https://other.example.test/campus-recruitment/demo/54046"
    assert rejected[0]["source_tenant"] == "web:other.example.test:/jobs"
    assert "secret" not in json.dumps(rejected, ensure_ascii=False)


def test_record_is_schema1_importer_compatible(monkeypatch) -> None:
    row = _row()
    candidate = "岗位职责\n" + "负责软件系统开发和测试。" * 8 + "\n任职要求\n本科及以上，熟悉 Python。"

    monkeypatch.setattr(
        batch,
        "fetch_job_detail_result_isolated",
        lambda *_args, **_kwargs: {
            "detail": candidate,
            "status": "complete",
            "source": "moka_official",
            "detail_url": row["detail_url"],
            "attempts": ["moka_detail_fields:jobDescription"],
            "error_type": "",
            "identity_status": "matched",
            "identity_evidence": ["native_id:route-id", "title:工程师-0"],
            "capture_evidence": {
                "status": "complete",
                "method": "moka_official",
                "source_url": row["detail_url"],
                "identity_verified": True,
                "terminal_observed": True,
                "remaining_controls": [],
                "content_sha256": batch.content_sha256(candidate),
            },
        },
    )
    item = batch._record_for_row(
        row,
        timeout_seconds=2,
        retries=0,
        deadline=time.monotonic() + 10,
        limiters=batch._HostLimiters(1),
    )

    assert item["selection"]["selected"] is True
    assert item["validation"]["passed"] is True
    assert item["candidate_jd"] == candidate
    assert item["candidate_sha256"]
    assert {"job_id", "original_sha256", "candidate_jd", "candidate_sha256", "validation"} <= item.keys()


def test_schema_probe_keeps_schema_and_hashes_without_body_values(monkeypatch) -> None:
    row = _row()
    payload = {
        "data": {
            "id": "0" * 32,
            "title": row["title"],
            "jobDescription": "真实正文不应落入 schema 报告",
            "requirements": "独立要求正文",
            "customFields": {"field-id": "敏感字段值"},
            "aimFields": [],
            "jobIntentions": [],
        }
    }

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 0}

    monkeypatch.setattr(
        job_details,
        "_moka_job_coordinates",
        lambda *_args, **_kwargs: ("https://app.mokahr.com/campus_apply/demo/54046", "0" * 32),
    )
    monkeypatch.setattr(
        job_details,
        "_moka_site_context",
        lambda _url: ("demo", 54046, "fedcba9876543210"),
    )
    monkeypatch.setattr(job_details, "_decode_moka_payload", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(batch.requests, "post", lambda *_args, **_kwargs: Response())

    result = batch._moka_schema_probe(row)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "ok"
    assert "jobDescription" in result["content_fields"]
    assert "requirements" in result["content_fields"]
    assert result["selected_fields"] == ["jobDescription", "requirements"]
    assert "真实正文不应落入 schema 报告" not in serialized
    assert "敏感字段值" not in serialized
