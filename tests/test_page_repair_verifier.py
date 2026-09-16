from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import verify_page_repair_samples as verifier


def _source(name: str = "大漠大智控") -> dict:
    return {
        "company": name,
        "aliases": [],
        "source_url": "https://example.test/campus",
        "crawler": "render",
        "lead_key": "baseline-lead",
        "source_projects": [name],
        "baseline_sha256": "a" * 64,
        "provenance": {"baseline_company": name, "baseline_lead_key": "baseline-lead", "baseline_source_projects": [name], "baseline_sha256": "a" * 64},
    }


def _job(index: int, text: str = "", **changes: object) -> dict:
    return {
        "id": f"job-{index}",
        "title": f"Engineer {index}",
        "jd_raw": text,
        "detail_url": f"https://example.test/job/{index}",
        **changes,
    }


def test_resolve_sources_uses_the_15_diagnosed_urls_and_evidence() -> None:
    sources = verifier.resolve_sources()
    assert len(sources) == 15
    assert len({row["source_url"] for row in sources}) == 15
    assert sources[0]["source_url"] == "https://www.dmduav.com/job/campus.html"
    assert sources[1]["source_url"].startswith("https://campus.cvte.com/project/")
    assert all(row["provenance"]["diagnosis"].startswith("docs/") for row in sources)
    raymx = next(row for row in sources if row["company"] == "沛睿微电子")
    assert raymx["lead_key"] == raymx["provenance"]["baseline_lead_key"]
    assert raymx["source_projects"] == raymx["provenance"]["baseline_source_projects"]
    assert len(raymx["baseline_sha256"]) == 64
    assert sum(row["provenance"]["round4_match"] for row in sources) == 10


def test_pagination_unknown_is_preserved_and_not_confirmed_incomplete() -> None:
    result = {"jobs": [], "pagination_complete": False, "completeness_known": False, "error_code": "pagination_evidence_missing"}
    assert verifier._pagination_state(result) == "unknown"
    assert verifier._pagination(result) == {
        "pagination_state": "unknown",
        "pagination_complete": False,
        "completeness_known": False,
        "pagination_evidence_missing": True,
        "pages_seen": None,
        "total_pages": None,
        "advertised_total": None,
        "has_more": None,
        "pagination_diagnostics": [],
    }
    assert verifier._classification(result, [], verifier._result_errors(result)) == "unknown"


def test_offline_run_is_bounded_and_hashes_jd_without_saving_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_list(company, *, timeout_seconds):
        calls.append(("list", company["name"]))
        return {
            "jobs": [_job(i) for i in range(4)],
            "raw_job_count": 4,
            "accepted_count": 4,
            "complete_jd_count": 0,
            "incomplete_jd_count": 4,
            "pagination_state": "complete",
            "pagination_complete": True,
            "completeness_known": True,
            "termination_reasons": ["end_of_pages"],
            "source_runs": [{"source_url": company["careers_url"], "pagination_complete": True, "project_diagnostics": [{"project_id": "current", "old_project_id": "legacy", "observed_project_id": "current", "observed_project_name": "Current project", "scope_changed": True, "page": 1, "total_pages": 2, "observed_unique": 4, "termination_reason": "total_reached", "cookie": "SECRET"}]}],
        }

    def fake_detail(job, *, timeout_seconds):
        calls.append(("detail", job["id"]))
        return {"detail": "Secret response body that must not be persisted", "status": "complete", "source": "mock", "identity_status": "matched", "identity_evidence": ["title:Engineer", "Secret response body"], "attempts": ["mock:complete"]}

    monkeypatch.setattr(verifier, "crawl_company_result_isolated", fake_list)
    monkeypatch.setattr(verifier, "fetch_job_detail_result_isolated", fake_detail)
    report = verifier.run_verification([_source()], tmp_path, concurrency=1, timeout_seconds=30, inputs={"offline": True})
    record = report["results"][0]
    assert [item[0] for item in calls] == ["list", "detail", "detail"]
    assert record["counts"]["missing_jd_total"] == 4
    assert record["counts"]["missing_jd_sampled"] == 2
    assert record["counts"]["missing_jd_not_sampled"] == 2
    assert all(sample["sample_scope"] == "sample" and sample["nonall"] for sample in record["jd_samples"])
    assert all("Secret response body" not in json.dumps(sample) for sample in record["jd_samples"])
    assert all(len(sample["original_jd_sha256"]) == 64 for sample in record["jd_samples"])
    assert all(sample["identity_status"] == "matched" for sample in record["jd_samples"])
    assert all(sample["source"] == "mock" and sample["attempts"] == ["mock:complete"] for sample in record["jd_samples"])
    project_diagnostics = record["sources"][1]["project_diagnostics"]
    assert project_diagnostics[0]["observed_project_id"] == "current"
    assert project_diagnostics[0]["observed_unique"] == 4
    assert "cookie" not in json.dumps(project_diagnostics)
    artifact = json.loads((tmp_path / record["job_artifact"]).read_text(encoding="utf-8"))
    assert set(artifact["jobs"][0]) == {"title", "id", "city", "jd_url", "jd_raw", "link_kind", "source_list_url", "source_url", "observed_proof"}
    assert "Secret response body" not in json.dumps(artifact)
    assert report["model_calls"] == report["database_writes"] == report["config_writes"] == 0
    assert report["modelcalls"] == report["dbwrites"] == report["configwrites"] == 0


def test_identity_and_public_moka_url_evidence_is_saved_without_refetching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source("BIGO")
    moka_url = "https://app.mokahr.com/campus_apply/bigo/1018/#/job/abc-123"
    job = _job(1, source_job_id="source-1", native_job_id="native-1", id=None, title="后端工程师", city="深圳", jd_url=moka_url, detail_url=moka_url, link_kind="detail", source_list_url=source["source_url"], source_url=source["source_url"], detail_link_observed=True, detail_link_source_url=source["source_url"])

    def fake_list(company, *, timeout_seconds):
        return {"jobs": [job], "pagination_state": "complete", "pagination_complete": True, "completeness_known": True, "termination_reasons": ["end_of_pages"]}

    def fake_detail(received, *, timeout_seconds):
        assert received["jd_url"] == moka_url
        return {"detail": "full jd", "status": "complete", "source": "moka", "identity_status": "matched", "identity_evidence": ["native_id:native-1", "title:后端工程师"], "attempts": ["moka:complete"]}

    monkeypatch.setattr(verifier, "crawl_company_result_isolated", fake_list)
    monkeypatch.setattr(verifier, "fetch_job_detail_result_isolated", fake_detail)
    report = verifier.run_verification([source], tmp_path, concurrency=1, timeout_seconds=30, inputs={"offline": True})
    record = report["results"][0]
    assert record["jd_inventory"][0]["job_id"] == "source-1"
    assert record["jd_inventory"][0]["job_id_field"] == "source_job_id"
    assert record["jd_inventory"][0]["title"] == "后端工程师"
    assert record["jd_inventory"][0]["jd_url"] == moka_url
    assert record["jd_samples"][0]["jd_url"] == moka_url
    assert record["jd_samples"][0]["hydrator"] == {"identity_status": "matched", "identity_evidence": ["native_id:native-1", "title:后端工程师"], "source": "moka", "attempts": ["moka:complete"]}
    artifact = json.loads((tmp_path / record["job_artifact"]).read_text(encoding="utf-8"))
    assert artifact["jobs"][0]["id"] == "source-1"
    assert artifact["jobs"][0]["jd_url"] == moka_url
    assert verifier._job_id({"native_job_id": "native-only"}) == ("native-only", "native_job_id")
    assert verifier._public_url("https://app.mokahr.com/campus_apply/bigo/1018/?access_token=secret#/job/abc-123") is None


def test_worker_payload_carries_frozen_oc_context() -> None:
    captured: dict = {}

    def fake_list(company, *, timeout_seconds):
        captured.update(company)
        return {"jobs": [], "pagination_state": "complete", "pagination_complete": True, "completeness_known": True, "termination_reasons": ["empty"]}

    row = verifier.verify_company(_source("沛睿微电子"), timeout_seconds=30, list_runner=fake_list, detail_runner=lambda *args, **kwargs: {})
    assert captured["source_cohort"] == 2027
    assert captured["source_cohort_source"] == verifier._oc_trusted_source()
    assert captured["source_cohort_url"] == "https://example.test/campus"
    assert "baseline_sha256=" + "a" * 64 in captured["source_cohort_evidence"]
    assert "lead_key=baseline-lead" in captured["source_cohort_evidence"]
    assert captured["source_projects"] == ["沛睿微电子"]
    assert row["source_context"] == {key: captured[key] for key in ("source_cohort", "source_cohort_source", "source_cohort_url", "source_cohort_evidence", "lead_key", "source_projects")}


def test_sample_selection_excludes_intern_doctorate_and_nonqualifying_batch() -> None:
    jobs = [
        _job(1, title="日常实习-开发工程师"),
        _job(2, title="博士研究员"),
        _job(3, title="Engineer", recruitment_track="social"),
        _job(4, title="正式开发工程师"),
    ]
    calls: list[str] = []

    def fake_list(company, *, timeout_seconds):
        return {"jobs": jobs, "pagination_state": "complete", "pagination_complete": True, "completeness_known": True, "termination_reasons": ["end_of_pages"]}

    def fake_detail(job, *, timeout_seconds):
        calls.append(job["id"])
        return {"detail": "full", "status": "complete", "identity_status": "matched", "identity_evidence": ["id:" + job["id"]], "source": "mock", "attempts": ["mock:complete"]}

    row = verifier.verify_company(_source(), timeout_seconds=30, list_runner=fake_list, detail_runner=fake_detail)
    assert calls == ["job-4"]
    assert row["counts"]["missing_jd_total"] == 4
    assert row["counts"]["missing_jd_excluded"] == row["counts"]["excluded_count"] == 3
    assert row["counts"]["missing_jd_sampled"] == 1
    assert row["jd_samples"][0]["title"] == "正式开发工程师"


def test_replay_reuses_company_artifact_and_skips_list_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source("沛睿微电子")
    old = tmp_path / "old-report"
    artifact = {
        "schema": verifier.SCHEMA,
        "company": source["company"],
        "source_url": source["source_url"],
        "jobs": [
            {"title": "日常实习-开发工程师", "id": "intern-1", "city": None, "jd_url": "https://example.test/job/intern-1", "jd_raw": "", "link_kind": "detail", "source_list_url": source["source_url"], "source_url": source["source_url"], "observed_proof": {"detail_link_observed": True, "detail_link_source_url": source["source_url"]}},
            {"title": "正式开发工程师", "id": "formal-1", "city": "深圳", "jd_url": "https://example.test/job/formal-1", "jd_raw": "", "link_kind": "detail", "source_list_url": source["source_url"], "source_url": source["source_url"], "observed_proof": {"detail_link_observed": True, "detail_link_source_url": source["source_url"]}},
        ],
    }
    verifier._write(old / "companies" / "raymx.json", artifact)
    verifier._write(old / "report.json", {"schema": verifier.SCHEMA, "results": [{"company": source["company"], "source_url": source["source_url"], "job_artifact": "companies/raymx.json", "pagination": {"pagination_state": "complete", "pagination_complete": True, "completeness_known": True, "pages_seen": 1, "total_pages": 1, "advertised_total": 2, "has_more": False, "pagination_diagnostics": [], "pagination_evidence_missing": False}, "counts": {"observed_jobs": 2}, "termination": ["end_of_pages"], "errors": [{"stage": "jd_sample", "code": "timeout"}, {"stage": "jd_sample", "code": "cohort_ineligible"}, {"stage": "list", "code": "pagination_incomplete"}], "sources": [{"source_url": source["source_url"], "kind": "requested"}], "jd_inventory": []}]})
    replay = verifier.load_replay(old, [source])
    list_calls: list[str] = []
    detail_calls: list[str] = []

    def forbidden_list(*args, **kwargs):
        list_calls.append("called")
        pytest.fail("replay must not start list crawling")

    def fake_detail(job, *, timeout_seconds):
        detail_calls.append(job["id"])
        return {"detail": "full", "status": "complete", "identity_status": "matched", "identity_evidence": ["id:" + job["id"]], "source": "mock", "attempts": ["mock:complete"]}

    monkeypatch.setattr(verifier, "crawl_company_result_isolated", forbidden_list)
    monkeypatch.setattr(verifier, "fetch_job_detail_result_isolated", fake_detail)
    report = verifier.run_verification([source], tmp_path / "new-report", concurrency=1, timeout_seconds=30, replay_rows=replay)
    row = report["results"][0]
    assert list_calls == []
    assert detail_calls == ["formal-1"]
    assert row["replay"] is True and row["list_fetch"] == "skipped_reused_artifact"
    assert row["counts"]["missing_jd_excluded"] == 1
    assert row["counts"]["missing_jd_sampled"] == 1
    assert {item["code"] for item in row["previous_jd_errors"]} == {"timeout", "cohort_ineligible"}
    assert [item["code"] for item in row["errors"]] == ["pagination_incomplete"]


def test_dry_run_does_not_start_runner_or_write_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setattr(verifier, "resolve_sources", lambda *args: [_source()])
    monkeypatch.setattr(verifier, "crawl_company_result_isolated", lambda *args, **kwargs: pytest.fail("runner started"))
    assert verifier.main(["--dry-run", "--company", "大漠大智控", "--output", str(tmp_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["will_start_isolated_workers"] is False
    assert not (tmp_path / "report.json").exists()


def test_timeout_does_not_leak_exception_text_or_mark_unknown_as_empty(tmp_path: Path) -> None:
    def timeout(*args, **kwargs):
        raise verifier.IsolatedOperationTimeout("Bearer SECRET-token response body")

    # Exercise the injected failure path explicitly; this test never starts a real worker.
    result = verifier.verify_company(_source(), timeout_seconds=30, list_runner=timeout)
    encoded = json.dumps(result)
    assert "SECRET-token" not in encoded
    assert result["pagination"]["pagination_state"] == "unknown"
    assert result["classification"] == "unknown"


@pytest.mark.parametrize("identity_status,evidence,verified", [
    ("unverified", [], False),
    ("matched", [], False),
    ("matched", ["title:Engineer"], True),
    ("request_bound", ["request_id:42"], True),
])
def test_text_completion_alone_is_not_verified_jd(identity_status, evidence, verified) -> None:
    def fake_detail(job, *, timeout_seconds):
        return {
            "detail": "fixture JD", "status": "complete", "source": "render",
            "identity_status": identity_status, "identity_evidence": evidence,
        }

    samples, errors, _, _ = verifier._sample(
        [_job(1)], {}, "https://example.test/campus", 30, fake_detail,
    )
    assert samples[0]["detail_status"] == "complete"
    assert samples[0]["sample_verified"] is verified
    assert [error["code"] for error in errors] == ([] if verified else ["identity_evidence_missing"])
