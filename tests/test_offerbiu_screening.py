from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
import unicodedata

import packages.matching.capture_screening as capture_screening
from packages.matching.capture_screening import (
    reconcile_passed_jsonl,
    screen_capture_jsonl,
    screen_capture_record,
)
from packages.matching.rules import content_fingerprint


def _evidence(text: str, url: str) -> dict[str, Any]:
    return {
        "status": "complete",
        "method": "official_api",
        "source_url": url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _job(
    *,
    company: str = "Acme Robotics",
    title: str = "C++ 软件开发工程师",
    url: str = "https://jobs.example.test/detail/job-1",
    native_id: str = "job-1",
    jd: str = "负责 C++ Linux 软件开发和工程实现。",
    tenant: str | None = "tenant-a",
    business_key: str | None = "bk-1",
    capture: bool = True,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "company": company,
        "title": title,
        "city": "上海",
        "job_type": "正式",
        "detail_url": url,
        "jd_raw": jd,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "native_job_id": native_id,
        "business_key": business_key,
    }
    if tenant is not None:
        value["source_tenant"] = tenant
    if capture:
        value["capture_evidence"] = _evidence(jd, url)
    return value


def _profile() -> dict[str, Any]:
    return {
        "direction": "C++软件开发",
        "matching": {"primary_directions": ["C++软件开发"]},
    }


def _hydration_row(
    *,
    capture_id: str = "capture-1",
    job: dict[str, Any] | None = None,
    candidate_jd_raw: str = "",
    candidate_capture_evidence: dict[str, Any] | None = None,
    company: str | None = None,
    hydration_outcome: str | None = None,
    hydration_status: str | None = None,
) -> dict[str, Any]:
    job = job or _job()
    row: dict[str, Any] = {
        "capture_id": capture_id,
        "job_key": f"source-{capture_id}",
        "company": company or job["company"],
        "job": job,
        "candidate_jd_raw": candidate_jd_raw,
        "candidate_capture_evidence": candidate_capture_evidence,
    }
    if hydration_outcome is not None:
        row["hydration_outcome"] = hydration_outcome
    if hydration_status is not None:
        row["hydration_status"] = hydration_status
    return row


def _passed_row(**kwargs: Any) -> dict[str, Any]:
    row = _hydration_row(**kwargs)
    state, passed = screen_capture_record(row, _profile())
    assert state["status"] == "passed"
    assert passed is not None
    return passed


def _snapshot(
    jobs: list[dict[str, Any]],
    analyses: list[dict[str, Any]] | None = None,
    companies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "companies": companies
        or [{"id": "company-1", "name": "Acme Robotics", "aliases": ["Acme机器人"]}],
        "jobs": jobs,
        "analyses": analyses or [],
        "application_audit": {"read_only": True},
        "read_only": True,
    }


def _write_reconcile_inputs(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    passed = tmp_path / "passed-only.jsonl"
    passed.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    snapshot_path = tmp_path / "catalog.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    output_dir = tmp_path / "reconcile"
    return passed, snapshot_path, output_dir


def _run_reconcile(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    passed, snapshot_path, output_dir = _write_reconcile_inputs(tmp_path, rows, snapshot)
    report = reconcile_passed_jsonl(passed, snapshot_path, output_dir)
    detail_path = output_dir / "reconciliation.jsonl"
    details = [json.loads(line) for line in detail_path.read_text(encoding="utf-8").splitlines()]
    return report, details, output_dir


def test_screen_uses_verified_candidate_jd_and_preserves_empty_candidate() -> None:
    original = _job(jd="旧正文")
    candidate = "新正文 C++ Linux 软件开发。"
    row = _hydration_row(
        job=original,
        candidate_jd_raw=candidate,
        candidate_capture_evidence=_evidence(candidate, original["detail_url"]),
        hydration_outcome="hydrated",
        hydration_status="complete",
    )
    state, passed = screen_capture_record(row, _profile())
    assert state["status"] == "passed"
    assert state["capture"]["jd_source"] == "candidate_hydration"
    assert passed is not None
    assert passed["job"]["jd_raw"] == candidate

    empty_state, empty_passed = screen_capture_record(
        _hydration_row(job=original, candidate_jd_raw="", candidate_capture_evidence=None),
        _profile(),
    )
    assert empty_state["status"] == "passed"
    assert empty_state["capture"]["jd_source"] == "original"
    assert empty_passed is not None
    assert empty_passed["job"]["jd_raw"] == "旧正文"


def test_screen_separates_excluded_and_deferred_with_reasons() -> None:
    excluded_job = _job(title="行政专员", jd="负责行政事务。")
    excluded_state, _ = screen_capture_record(_hydration_row(job=excluded_job), _profile())
    assert excluded_state["status"] == "excluded"
    assert excluded_state["rule_status"] == "direction_out"
    assert excluded_state["reasons"] == ["direction_out"]

    unclear_job = _job(title="工程师", jd="负责项目协作与交付管理。")
    unclear_state, _ = screen_capture_record(_hydration_row(job=unclear_job), _profile())
    assert unclear_state["status"] == "deferred"
    assert "direction_unclear" in unclear_state["reasons"]

    incomplete_job = _job(capture=False)
    incomplete_state, _ = screen_capture_record(_hydration_row(job=incomplete_job), _profile())
    assert incomplete_state["status"] == "deferred"
    assert incomplete_state["rule_status"] == "jd_incomplete"
    assert "jd_incomplete" in incomplete_state["reasons"]


def test_exact_identity_reuses_scored_analysis_even_with_old_version(tmp_path: Path) -> None:
    job = _job()
    snapshot_job = {"id": "existing-1", "company_id": "company-1", **job}
    snapshot = _snapshot(
        [snapshot_job],
        [
            {
                "job_id": "existing-1",
                "analysis_status": "complete",
                "match_score": 88,
                "content_fingerprint": content_fingerprint(snapshot_job),
                "analysis_version": "old-model",
                "model": "old-model-name",
            }
        ],
    )
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=job)], snapshot)
    assert report["counts"]["scored"] == 1
    assert report["counts"]["changed"] == 0
    assert details[0]["existing_job_id"] == "existing-1"
    assert details[0]["bucket"] == "scored"
    assert details[0]["analysis"]["reuse_existing_score"] is True


def test_legacy_job_without_business_key_matches_by_normalized_url(tmp_path: Path) -> None:
    current = _job(url="https://jobs.example.test/detail/job-1?utm_source=new", business_key="new-bk")
    old = _job(url="https://jobs.example.test/detail/job-1?utm_source=old", business_key=None)
    old["id"] = "existing-legacy"
    old["company_id"] = "company-1"
    snapshot = _snapshot(
        [old],
        [{"job_id": "existing-legacy", "analysis_status": "complete", "match_score": 70}],
    )
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=current)], snapshot)
    assert report["counts"]["scored"] == 1
    assert details[0]["existing_job_id"] == "existing-legacy"
    assert "canonical_url" in details[0]["match_method"]


def test_explicit_company_alias_confirms_same_company(tmp_path: Path) -> None:
    current = _job(company="Acme机器人")
    old = _job(company="Acme Robotics")
    old["id"] = "existing-alias"
    old["company_id"] = "company-1"
    snapshot = _snapshot(
        [old],
        [{"job_id": "existing-alias", "analysis_status": "complete", "match_score": 81}],
    )
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=current, company="Acme机器人")], snapshot)
    assert report["counts"]["scored"] == 1
    assert details[0]["existing_job_id"] == "existing-alias"


def test_same_native_id_across_tenants_is_not_merged(tmp_path: Path) -> None:
    current = _job(tenant="tenant-b", url="https://tenant-b.example.test/job/42", native_id="42")
    old = _job(
        tenant="tenant-a",
        url="https://tenant-a.example.test/job/42",
        native_id="42",
        business_key="bk-tenant-a",
    )
    old["id"] = "existing-tenant-a"
    old["company_id"] = "company-1"
    snapshot = _snapshot([old])
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=current)], snapshot)
    assert report["counts"]["new"] == 1
    assert details[0]["existing_job_id"] is None
    assert details[0]["bucket"] == "new"


def test_shared_portal_same_id_across_companies_is_identity_pending(tmp_path: Path) -> None:
    first = _job(company="Company A", tenant="shared-portal", native_id="same-id", url="https://shared.test/job/a")
    first["id"] = "existing-a"
    first["company_id"] = "company-a"
    second = _job(company="Company B", tenant="shared-portal", native_id="same-id", url="https://shared.test/job/b")
    second["id"] = "existing-b"
    second["company_id"] = "company-b"
    companies = [
        {"id": "company-a", "name": "Company A"},
        {"id": "company-b", "name": "Company B"},
        {"id": "company-c", "name": "Company C"},
    ]
    current = _job(company="Company C", tenant="shared-portal", native_id="same-id", url="https://shared.test/job/c")
    snapshot = _snapshot([first, second], companies=companies)
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=current, company="Company C")], snapshot)
    assert report["counts"]["identity_pending"] == 1
    assert details[0]["existing_job_id"] is None
    assert "same_native_or_url_across_companies_not_merged" in details[0]["reasons"]


def test_same_title_with_different_identity_is_new_not_merged(tmp_path: Path) -> None:
    old = _job(native_id="old-id", url="https://jobs.example.test/detail/old-id")
    old["id"] = "existing-old"
    old["company_id"] = "company-1"
    current = _job(
        native_id="new-id",
        url="https://jobs.example.test/detail/new-id",
        business_key="bk-new",
    )
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=current)], _snapshot([old]))
    assert report["counts"]["new"] == 1
    assert details[0]["existing_job_id"] is None
    assert details[0]["title_conflicts"] == ["existing-old"]
    assert "same_title_not_merged" in details[0]["reasons"]


def test_jd_change_is_separate_and_empty_candidate_does_not_replace_old(tmp_path: Path) -> None:
    old = _job(jd="旧 JD")
    old["id"] = "existing-jd"
    old["company_id"] = "company-1"
    changed = _job(jd="新 JD C++ Linux")
    snapshot = _snapshot(
        [old],
        [
            {
                "job_id": "existing-jd",
                "analysis_status": "complete",
                "match_score": 75,
                "content_fingerprint": content_fingerprint(old),
            }
        ],
    )
    report, details, _ = _run_reconcile(tmp_path, [_passed_row(job=changed)], snapshot)
    assert report["counts"]["changed"] == 1
    assert details[0]["analysis"]["content_changed"] is True

    preserved = _job(jd="旧 JD")
    row = _hydration_row(job=preserved, candidate_jd_raw="", candidate_capture_evidence=None)
    state, passed = screen_capture_record(row, _profile())
    assert state["capture"]["jd_source"] == "original"
    assert passed is not None
    report, details, _ = _run_reconcile(tmp_path / "empty", [_passed_row(job=preserved)], snapshot)
    assert report["counts"]["scored"] == 1
    assert details[0]["analysis"]["content_changed"] is False
    assert details[0]["analysis"]["change_reason"] == "content_unchanged"


def test_batch_duplicates_and_report_are_idempotent(tmp_path: Path) -> None:
    job = _job()
    old = {"id": "existing-1", "company_id": "company-1", **job}
    snapshot = _snapshot(
        [old],
        [{"job_id": "existing-1", "analysis_status": "complete", "match_score": 80}],
    )
    first_report, _, output_dir = _run_reconcile(
        tmp_path,
        [_passed_row(capture_id="a", job=job), _passed_row(capture_id="b", job=job)],
        snapshot,
    )
    first_bytes = (output_dir / "reconciliation-report.json").read_bytes()
    second_report = reconcile_passed_jsonl(
        tmp_path / "passed-only.jsonl",
        tmp_path / "catalog.json",
        output_dir,
    )
    second_bytes = (output_dir / "reconciliation-report.json").read_bytes()
    assert first_report["counts"]["batch_duplicate_records"] == 1
    assert second_report == first_report
    assert second_bytes == first_bytes


def test_screen_then_reconcile_uses_only_passed_rows(tmp_path: Path) -> None:
    candidate = "新正文 C++ Linux 软件开发。"
    good_job = _job(jd="稀疏旧正文")
    rows = [
        _hydration_row(
            capture_id="good",
            job=good_job,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=_evidence(candidate, good_job["detail_url"]),
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _hydration_row(capture_id="excluded", job=_job(title="行政专员", jd="行政工作。")),
    ]
    input_path = tmp_path / "jobs.jsonl"
    input_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    screen_dir = tmp_path / "screen"
    summary = screen_capture_jsonl(input_path, screen_dir, _profile())
    assert summary["counts"]["passed"] == 1
    assert summary["counts"]["excluded"] == 1
    passed_rows = [json.loads(line) for line in (screen_dir / "passed-only.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(passed_rows) == 1
    assert passed_rows[0]["job"]["jd_raw"] == candidate

    existing = {"id": "existing-good", "company_id": "company-1", **good_job}
    report, details, _ = _run_reconcile(
        tmp_path / "final",
        passed_rows,
        _snapshot([existing], [{"job_id": "existing-good", "analysis_status": "complete", "match_score": 90}]),
    )
    assert report["records_read"] == 1
    assert details[0]["existing_job_id"] == "existing-good"


def test_candidate_assessment_receives_complete_job_and_requires_successful_hydration(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    real_assessor = capture_screening.assess_jd_capture

    def spy(job: Any) -> Any:
        seen.append(dict(job))
        return real_assessor(job)

    monkeypatch.setattr(capture_screening, "assess_jd_capture", spy)
    original = _job(jd="旧正文")
    candidate = "新正文 C++ Linux 软件开发。"
    state, passed = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=_evidence(candidate, original["detail_url"]),
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _profile(),
    )
    assert state["capture"]["candidate_used"] is True
    assert passed is not None
    assert any(
        item.get("jd_raw") == candidate
        and item.get("title") == original["title"]
        and item.get("native_job_id") == original["native_job_id"]
        and item.get("detail_url") == original["detail_url"]
        for item in seen
    )


def test_candidate_receipt_cannot_be_borrowed_by_another_job() -> None:
    original = _job(jd="旧正文")
    candidate = "新正文 C++ Linux 软件开发。"
    state, passed = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=_evidence(
                candidate,
                "https://jobs.example.test/detail/another-job",
            ),
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _profile(),
    )
    assert state["capture"]["jd_source"] == "original"
    assert state["capture"]["candidate_used"] is False
    assert state["capture"]["candidate_assessment"]["reason_code"] == "capture_source_mismatch"
    assert passed is not None
    assert passed["job"]["jd_raw"] == "旧正文"


def test_candidate_receipt_rejects_contradictory_native_ids_and_titles() -> None:
    original = _job(jd="旧正文")
    candidate = "新正文 C++ Linux 软件开发。"
    native_conflict = _evidence(candidate, original["detail_url"])
    native_conflict["identity_evidence"] = ["native_id:job-1", "native_id:wrong-job"]
    state, _ = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=native_conflict,
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _profile(),
    )
    assert state["capture"]["candidate_assessment"]["reason_code"] == "capture_native_id_conflict"

    title_conflict = _evidence(candidate, original["detail_url"])
    title_conflict["identity_evidence"] = [
        f"title:{original['title']}",
        "title:另一个岗位",
    ]
    state, _ = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=title_conflict,
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _profile(),
    )
    assert state["capture"]["candidate_assessment"]["reason_code"] == "capture_title_conflict"

    separate_namespace = _evidence(candidate, original["detail_url"])
    separate_namespace["identity_evidence"] = ["native_id:job-1", "job_id:catalog-row-99"]
    state, _ = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=separate_namespace,
            hydration_outcome="hydrated",
            hydration_status="complete",
        ),
        _profile(),
    )
    assert state["capture"]["candidate_used"] is True


def test_candidate_with_failed_hydration_outcome_is_not_used() -> None:
    original = _job(jd="旧正文 C++ Linux")
    candidate = "新正文 C++ Linux 软件开发。"
    state, passed = screen_capture_record(
        _hydration_row(
            job=original,
            candidate_jd_raw=candidate,
            candidate_capture_evidence=_evidence(candidate, original["detail_url"]),
            hydration_outcome="failed",
            hydration_status="failed",
        ),
        _profile(),
    )
    assert state["capture"]["jd_source"] == "original"
    assert state["capture"]["hydration_succeeded"] is False
    assert state["capture"]["candidate_assessment"]["reason_code"] == "hydration_outcome_not_success"
    assert passed is not None
    assert passed["job"]["jd_raw"] == original["jd_raw"]


def test_score_reuse_requires_complete_status_and_valid_finite_range(tmp_path: Path) -> None:
    jobs: list[dict[str, Any]] = []
    analyses: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    analysis_values = [
        ("failed", 88),
        ("complete", 101),
        ("complete", float("nan")),
    ]
    for index, (status, score) in enumerate(analysis_values, start=1):
        job = _job(
            native_id=f"score-{index}",
            url=f"https://jobs.example.test/detail/score-{index}",
            business_key=f"score-bk-{index}",
            jd=f"旧 JD C++ Linux {index}",
        )
        current = _job(
            native_id=f"score-{index}",
            url=f"https://jobs.example.test/detail/score-{index}",
            business_key=f"score-bk-{index}",
            jd=f"旧 JD C++ Linux {index}",
        )
        job.update(id=f"existing-score-{index}", company_id="company-1")
        jobs.append(job)
        analyses.append(
            {
                "job_id": f"existing-score-{index}",
                "analysis_status": status,
                "match_score": score,
            }
        )
        rows.append(_passed_row(capture_id=f"score-{index}", job=current))

    report, details, _ = _run_reconcile(tmp_path, rows, _snapshot(jobs, analyses))
    assert report["counts"]["pending"] == 3
    assert report["counts"]["scored"] == 0
    for detail in details:
        assert detail["analysis"]["scored"] is False
        assert detail["analysis"]["reuse_existing_score"] is False
        assert detail["analysis"]["score_preserved"] == (detail["analysis"]["match_score"] is not None)
        assert detail["reasons"] == ["existing_analysis_incomplete_or_score_invalid"]


def test_identity_conflicts_are_pending_and_unknown_detail_id_is_not_guessed(tmp_path: Path) -> None:
    controlled_url = "https://app.mokahr.com/campus_apply/acme/1#/job/123"
    conflicting = _job(
        url=controlled_url,
        native_id="999",
        business_key="moka-bk",
    )
    report, details, _ = _run_reconcile(
        tmp_path / "controlled",
        [_passed_row(job=conflicting)],
        _snapshot([]),
    )
    assert report["counts"]["identity_pending"] == 1
    assert "url_native_id_conflict" in details[0]["reasons"]

    current = _job(
        url="https://jobs.example.test/detail/shared",
        native_id="new-native",
        business_key="shared-bk",
    )
    old = _job(
        url="https://jobs.example.test/detail/shared",
        native_id="old-native",
        business_key="shared-bk",
    )
    old.update(id="existing-conflict", company_id="company-1")
    report, details, _ = _run_reconcile(
        tmp_path / "shared",
        [_passed_row(job=current)],
        _snapshot([old]),
    )
    assert report["counts"]["identity_pending"] == 1
    assert "url_native_id_conflict" in details[0]["reasons"]
    assert capture_screening._native_id_from_url("https://unknown.example/detail/42") == ""


def test_reproducible_legacy_synthetic_id_is_normalized_read_only(tmp_path: Path) -> None:
    url = "https://app.mokahr.com/campus_apply/yanhun/24017#/job/c58ebf8a-7b46-42cc-9c8c-0b005a2d01ab"
    old = _job(url=url, native_id="placeholder", business_key=None, jd="C++ Linux 旧正文")
    old.update(id="placeholder", company_id="company-1")
    encoded = "\x00".join(
        " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
        for value in (old["company_id"], old["detail_url"], old["title"], old["city"])
    )
    synthetic = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    old.update(id=synthetic, native_job_id=synthetic)
    current = _job(url=url, native_id="c58ebf8a-7b46-42cc-9c8c-0b005a2d01ab", business_key=None, jd="C++ Linux 旧正文")
    report, details, _ = _run_reconcile(
        tmp_path / "known-route",
        [_passed_row(job=current)],
        _snapshot(
            [old],
            [{"job_id": synthetic, "analysis_status": "complete", "match_score": 86}],
        ),
    )
    assert report["counts"]["scored"] == 1
    assert details[0]["existing_job_id"] == synthetic
    assert details[0]["existing_identity"]["legacy_synthetic"] is True
    assert details[0]["existing_identity"]["legacy_synthetic_native_job_id"] == synthetic
    assert details[0]["identity"]["native_job_id"] == current["native_job_id"]

    unknown_url = "https://unknown.example/detail/legacy-job"
    old_unknown = _job(url=unknown_url, native_id="placeholder", business_key=None, jd="C++ Linux 旧正文")
    old_unknown.update(id="placeholder", company_id="company-1")
    encoded_unknown = "\x00".join(
        " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
        for value in (
            old_unknown["company_id"],
            old_unknown["detail_url"],
            old_unknown["title"],
            old_unknown["city"],
        )
    )
    unknown_synthetic = hashlib.sha256(encoded_unknown.encode("utf-8")).hexdigest()
    old_unknown.update(id=unknown_synthetic, native_job_id=unknown_synthetic)
    current_unknown = _job(
        url=unknown_url,
        native_id=None,
        business_key=None,
        jd="C++ Linux 旧正文",
    )
    report, details, _ = _run_reconcile(
        tmp_path / "unknown-route",
        [_passed_row(job=current_unknown)],
        _snapshot(
            [old_unknown],
            [{"job_id": unknown_synthetic, "analysis_status": "complete", "match_score": 77}],
        ),
    )
    assert report["counts"]["scored"] == 1
    assert details[0]["match_method"] == "canonical_url"
    assert details[0]["identity"]["native_job_id"] is None
    assert details[0]["identity"]["legacy_synthetic"] is False


def test_non_reproducible_legacy_hex_id_remains_a_real_conflict(tmp_path: Path) -> None:
    url = "https://app.mokahr.com/campus_apply/acme/1#/job/123"
    arbitrary = "f" * 64
    old = _job(url=url, native_id=arbitrary, business_key=None, jd="C++ Linux 旧正文")
    old.update(id=arbitrary, company_id="company-1")
    current = _job(url=url, native_id="123", business_key=None, jd="C++ Linux 旧正文")
    report, details, _ = _run_reconcile(
        tmp_path,
        [_passed_row(job=current)],
        _snapshot(
            [old],
            [{"job_id": arbitrary, "analysis_status": "complete", "match_score": 91}],
        ),
    )
    assert report["counts"]["identity_pending"] == 1
    assert details[0]["existing_job_id"] is None
    assert details[0]["identity"]["legacy_synthetic"] is False
    assert "identity_conflict" in details[0]["reasons"]


def test_batch_new_company_conflict_defers_all_sources_without_scoring_groups(tmp_path: Path) -> None:
    first = _passed_row(capture_id="source-a", job=_job(company="Company A"))
    second = _passed_row(capture_id="source-b", job=_job(company="Company A"))
    third = _passed_row(capture_id="source-c", job=_job(company="Company B"))
    report, details, output_dir = _run_reconcile(
        tmp_path,
        [first, second, third],
        _snapshot(
            [],
            companies=[
                {"id": "company-1", "name": "Acme Robotics"},
                {"id": "company-a", "name": "Company A"},
                {"id": "company-b", "name": "Company B"},
            ],
        ),
    )
    assert report["counts"]["new"] == 0
    assert report["counts"]["identity_pending"] == 3
    assert report["counts"]["batch_duplicate_records"] == 2
    assert report["source_row_counts"]["passed_source_rows"] == 3
    assert report["source_row_counts"]["unique_new_groups"] == 0
    assert report["source_row_counts"]["identity_conflict_groups"] == 1
    assert all(not detail.get("distinct_work_key") for detail in details)
    assert all("batch_company_identity_conflict" in detail["reasons"] for detail in details)
    groups = [
        json.loads(line)
        for line in (output_dir / "groups.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert groups == []
    conflicts_path = output_dir / "batch-conflicts.jsonl"
    conflicts = [json.loads(line) for line in conflicts_path.read_text(encoding="utf-8").splitlines()]
    assert len(conflicts) == 1
    assert conflicts[0]["reclassified_new_source_rows"] == 3
    assert {source["capture_id"] for source in conflicts[0]["sources"]} == {
        "source-a",
        "source-b",
        "source-c",
    }
    assert report["potential_scoring_work"]["total"] == 0
    before = conflicts_path.read_bytes()
    repeated = reconcile_passed_jsonl(tmp_path / "passed-only.jsonl", tmp_path / "catalog.json", output_dir)
    assert repeated == report
    assert conflicts_path.read_bytes() == before


def test_batch_company_aliases_share_one_safe_new_group(tmp_path: Path) -> None:
    rows = [
        _passed_row(capture_id="canonical", job=_job(company="Acme Robotics")),
        _passed_row(capture_id="alias", job=_job(company="Acme机器人")),
    ]
    report, details, output_dir = _run_reconcile(tmp_path, rows, _snapshot([]))
    assert report["counts"]["new"] == 2
    assert report["source_row_counts"]["unique_new_groups"] == 1
    assert report["batch_company_identity_conflicts"]["identity_components"] == 0
    assert details[0]["distinct_work_key"] == details[1]["distinct_work_key"]
    groups = [json.loads(line) for line in (output_dir / "groups.jsonl").read_text(encoding="utf-8").splitlines()]
    assert groups[0]["source_count"] == 2
    assert {source["company"] for source in groups[0]["sources"]} == {"Acme Robotics", "Acme机器人"}


def test_batch_same_native_id_in_different_tenants_stays_independent(tmp_path: Path) -> None:
    rows = [
        _passed_row(
            capture_id=tenant,
            job=_job(company=tenant, tenant=tenant, native_id="42", business_key=None,
                     url=f"https://{tenant}.jobs.feishu.cn/campus/position/42/detail"),
        )
        for tenant in ("company-a", "company-b")
    ]
    report, details, _ = _run_reconcile(tmp_path, rows, _snapshot([]))
    assert report["counts"]["new"] == 2
    assert report["source_row_counts"]["unique_new_groups"] == 2
    assert report["batch_company_identity_conflicts"]["identity_components"] == 0
    assert details[0]["distinct_work_key"] != details[1]["distinct_work_key"]


def test_batch_company_conflict_checks_url_when_preferred_identity_differs(tmp_path: Path) -> None:
    url = "https://jobs.example.test/detail/shared"
    rows = [
        _passed_row(capture_id="native", job=_job(company="Company A", url=url, native_id="42")),
        _passed_row(capture_id="url", job=_job(company="Company B", url=url, native_id=None)),
        _passed_row(capture_id="other-url", job=_job(
            company="Company A", url="https://jobs.example.test/detail/another-route", native_id="42")),
    ]
    report, details, _ = _run_reconcile(tmp_path, rows, _snapshot([]))
    assert report["counts"]["new"] == 0
    assert report["counts"]["identity_pending"] == 3
    assert report["batch_company_identity_conflicts"]["identity_components"] == 1
    assert all("batch_company_identity_conflict" in detail["reasons"] for detail in details)


def test_batch_untrusted_company_label_preserves_unique_catalog_match(tmp_path: Path) -> None:
    old = {**_job(), "id": "trusted-job", "company_id": "company-1"}
    rows = [
        _passed_row(capture_id="trusted", job=_job()),
        _passed_row(capture_id="untrusted", job=_job(company="Unverified Company")),
    ]
    report, details, _ = _run_reconcile(
        tmp_path, rows,
        _snapshot([old], [{"job_id": "trusted-job", "analysis_status": "complete", "match_score": 80}]),
    )
    assert report["counts"]["scored"] == 1
    assert report["counts"]["identity_pending"] == 1
    assert details[0]["existing_job_id"] == "trusted-job"
    assert details[0]["analysis"]["reuse_existing_score"] is True
    assert report["distinct_job_counts"]["existing_scored"] == 1


def test_saved_distinct_work_counts_deduplicate_changed_and_pending(tmp_path: Path) -> None:
    unscored = _job(native_id="unscored", business_key="unscored", url="https://jobs.example.test/detail/unscored")
    scored = _job(native_id="scored", business_key="scored", url="https://jobs.example.test/detail/scored")
    jobs = [
        {**unscored, "id": "old-unscored", "company_id": "company-1"},
        {**scored, "id": "old-scored", "company_id": "company-1"},
    ]
    rows = [
        _passed_row(capture_id="unchanged-unscored", job=unscored),
        _passed_row(capture_id="unchanged-scored", job=scored),
    ]
    for name, job in (("unscored", unscored), ("scored", scored)):
        for number in range(2):
            changed = {**job, "jd_raw": "C++ Linux changed content"}
            changed["capture_evidence"] = _evidence(changed["jd_raw"], changed["detail_url"])
            rows.append(_passed_row(capture_id=f"changed-{name}-{number}", job=changed))
    for number in range(2):
        rows.append(_passed_row(capture_id=f"new-{number}", job=_job()))
    for company in ("Company A", "Company B"):
        rows.append(_passed_row(capture_id=company, job=_job(
            company=company, native_id="conflict", business_key="conflict",
            url="https://jobs.example.test/detail/conflict")))
    report, _, output_dir = _run_reconcile(tmp_path, rows, _snapshot(jobs, [
        {"job_id": "old-unscored", "analysis_status": "failed", "match_score": 70},
        {"job_id": "old-scored", "analysis_status": "complete", "match_score": 80},
    ]))
    saved = json.loads((output_dir / "reconciliation-report.json").read_text(encoding="utf-8"))
    assert saved["distinct_job_counts"] == {
        "new_unique_groups": 1,
        "existing_total": 2, "existing_scored": 1, "existing_unscored": 1,
        "changed_total": 2, "changed_scored": 1, "changed_unscored": 1,
        "pending_bucket": 1, "scored_bucket": 1,
    }
    assert report["counts"]["changed"] == 4
    assert saved["potential_scoring_work"]["total"] == 2
    assert saved["potential_scoring_work"]["identity_pending_excluded"] is True
    assert saved["potential_scoring_work"]["existing_unscored_unique_jobs"] == 1


def test_changed_report_is_two_dimensional_and_keeps_old_scores(tmp_path: Path) -> None:
    jobs: list[dict[str, Any]] = []
    analyses: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    values = [("complete", 80), ("failed", 70), ("complete", -1)]
    for index, (status, score) in enumerate(values, start=1):
        old = _job(
            native_id=f"matrix-{index}",
            url=f"https://jobs.example.test/detail/matrix-{index}",
            business_key=f"matrix-bk-{index}",
            jd=f"旧 JD {index}",
        )
        old.update(id=f"matrix-existing-{index}", company_id="company-1")
        jobs.append(old)
        analyses.append(
            {
                "job_id": old["id"],
                "analysis_status": status,
                "match_score": score,
            }
        )
        rows.append(
            _passed_row(
                capture_id=f"matrix-{index}",
                job=_job(
                    native_id=f"matrix-{index}",
                    url=f"https://jobs.example.test/detail/matrix-{index}",
                    business_key=f"matrix-bk-{index}",
                    jd=f"新 JD C++ Linux {index}",
                ),
            )
        )

    report, details, _ = _run_reconcile(tmp_path, rows, _snapshot(jobs, analyses))
    assert report["counts"]["changed"] == 3
    assert report["changed_report"] == {
        "total": 3,
        "scored": 1,
        "unscored": 2,
        "score_preserved": 3,
        "default_rescore": 0,
        "matrix": report["changed_matrix"],
    }
    assert report["changed_matrix"]["complete"]["content_changed"]["scored"] == 1
    assert report["changed_matrix"]["complete"]["content_changed"]["unscored"] == 1
    assert report["changed_matrix"]["not_complete"]["content_changed"]["unscored"] == 1
    assert sorted(detail["analysis"]["match_score"] for detail in details) == [-1, 70, 80]
    assert all(detail["bucket"] == "changed" for detail in details)
