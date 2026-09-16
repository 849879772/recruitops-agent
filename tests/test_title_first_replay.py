from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest

from packages.matching import title_first_replay as module


def test_installed_title_policy_is_title_only():
    policy = module._load_title_policy()

    assert policy.normalize_job_title_key("  C++   工程师 ") == "C++ 工程师"
    assert policy.company_title_key("company-1", "  C++   工程师 ") == ("company-1", "C++ 工程师")
    assert policy.screen_title_job({"title": "C++ 工程师", "jd_raw": "博士"}).eligible is True
    assert policy.screen_title_job({"title": "实习 C++ 工程师", "jd_raw": ""}).eligible is False
    assert policy.screen_title_job({"title": "博士 C++ 工程师", "jd_raw": ""}).eligible is False


def _title_policy(monkeypatch):
    calls: list[dict[str, Any]] = []

    def normalize(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    def company_title_key(company_id: Any, title: Any) -> tuple[str, str]:
        return (str(company_id), normalize(title))

    def screen(job: Any, profile: Any = None) -> dict[str, Any]:
        assert profile is None or isinstance(profile, dict)
        calls.append(dict(job))
        title = normalize(job.get("title"))
        lowered = title.casefold()
        excluded = "实习" in title or "博士" in title
        eligible = not excluded and any(token in lowered for token in ("c++", "机器人", "agent", "llm"))
        return {
            "eligible": eligible,
            "analysis_status": "eligible" if eligible else "internship_or_doctorate",
            "reasons": [] if eligible else ["title_excluded"],
            "evidence": [{"source": "title", "signal": "fixture", "excerpt": title}],
            "matched_directions": [],
            "primary_match_direction": None,
            "supporting_evidence": [],
        }

    monkeypatch.setattr(module, "_load_title_policy", lambda: module._TitlePolicy(normalize, company_title_key, screen))
    return calls


def _evidence(detail: str, url: str, title: str) -> dict[str, Any]:
    return {
        "status": "complete",
        "method": "official_api",
        "captured_at": "2026-09-08T00:00:00+08:00",
        "source_url": url,
        "detail_url": url,
        "identity_verified": True,
        "identity_evidence": [f"native_id:fixture-{title}", f"title:{title}"],
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.encode("utf-8")).hexdigest(),
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _checkpoint(
    path: Path,
    source_key: str,
    company: str,
    jobs: list[dict[str, Any]],
    *,
    status: str = "complete",
    pagination_complete: bool = True,
    completeness_known: bool = True,
    reason_code: str | None = None,
    source_runs: list[dict[str, Any]] | None = None,
) -> None:
    _write_json(
        path,
        {
            "task_key": source_key,
            "company": company,
            "crawler_status": status,
            "reason_code": reason_code,
            "crawl_url": f"https://{source_key}.example/campus",
            "crawl": {
                "raw_jobs": jobs,
                "pagination_evidence": {
                    "pagination_complete": pagination_complete,
                    "completeness_known": completeness_known,
                    "pages_seen": 0,
                    "total_pages": None,
                    "has_more": False,
                    "advertised_total": None,
                },
                "source_runs": source_runs or [{"termination_reason": "legacy_all_pages"}],
            },
        },
    )


def _base_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    sources = tmp_path / "sources.json"
    checkpoints = tmp_path / "checkpoints"
    hydration = tmp_path / "hydration" / "jobs.jsonl"
    catalog = tmp_path / "catalog.json"
    _write_json(
        sources,
        [
            {
                "key": "acme-source",
                "row": {
                    "companyId": "c-acme",
                    "companyName": "Acme",
                    "applyUrl": "https://acme.example/campus",
                    "targetYears": [2027],
                    "recruitType": "秋招",
                },
            },
            {
                "key": "no-entry-source",
                "row": {"companyId": "c-empty", "companyName": "No Entry", "applyUrl": ""},
            },
        ],
    )
    checkpoints.mkdir(parents=True)
    _checkpoint(
        checkpoints / "acme.json",
        "acme-source",
        "Acme",
        [
            {
                "title": "C++ 工程师",
                "detail_url": "https://acme.example/job/existing",
                "native_job_id": "old-1",
                "cohort": 2027,
                "cohort_status": "confirmed",
                "jd_raw": "list text must not replace catalog",
            },
            {
                "title": "机器人 软件工程",
                "detail_url": "https://acme.example/job/robot-1",
                "native_job_id": "robot-a",
                "cohort": "unknown",
                "cohort_status": "unknown",
                "jd_raw": "list-only text",
            },
            {
                "title": " 机器人   软件工程 ",
                "detail_url": "https://acme.example/job/robot-2",
                "native_job_id": "robot-b",
                "cohort": 235,
                "cohort_status": "legacy",
                "jd_raw": "duplicate list-only text",
            },
            {
                "title": "Agent 工程师",
                "detail_url": "https://acme.example/job/agent-1",
                "cohort": 2027,
                "cohort_status": "confirmed",
            },
            {
                "title": "LLM 工程师",
                "detail_url": "https://acme.example/job/llm-1",
                "cohort": "unknown",
                "cohort_status": "unknown",
            },
            {"title": "实习 C++ 工程师", "detail_url": "https://acme.example/job/intern"},
            {"title": "博士 Agent 工程师", "detail_url": "https://acme.example/job/phd"},
        ],
    )
    _checkpoint(
        checkpoints / "empty.json",
        "no-entry-source",
        "No Entry",
        [],
        status="skipped",
        pagination_complete=False,
        completeness_known=False,
        reason_code="missing_entry",
    )
    _write_jsonl(
        hydration,
        [
            {
                "company": "Acme",
                "job": {
                    "title": "机器人 软件工程",
                    "detail_url": "https://acme.example/job/robot-1",
                    "cohort": 2027,
                    "cohort_status": "unknown",
                },
                "hydration_outcome": "hydrated",
                "detail": "official robot JD",
                "capture_evidence": _evidence(
                    "official robot JD", "https://acme.example/job/robot-1", "机器人 软件工程"
                ),
            },
            {
                "company": "Acme",
                "job": {"title": "Agent 工程师", "detail_url": "https://acme.example/job/agent-1"},
                "hydration_outcome": "fetch_failed",
                "request_made": True,
                "error_code": "timeout",
            },
            {
                "company": "Acme",
                "job": {"title": "LLM 工程师", "detail_url": "https://acme.example/job/llm-1"},
                "hydration_outcome": "cohort_unconfirmed_not_hydrated",
                "request_made": False,
            },
            {
                "company": "Acme",
                "job": {"title": "C++ 工程师", "detail_url": "https://acme.example/job/existing"},
                "hydration_outcome": "fetch_failed",
                "request_made": True,
                "error": "must be ignored because catalog title exists",
            },
        ],
    )
    _write_json(
        catalog,
        {
            "companies": [{"id": "c-acme", "name": "Acme", "aliases": []}],
            "jobs": [
                {
                    "id": "existing-1",
                    "company_id": "c-acme",
                    "company": "Acme",
                    "title": "C++ 工程师",
                    "detail_url": "https://catalog.example/existing",
                    "jd_raw": "catalog text",
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "capture_status": "failed",
                    "capture_failure_reason": "old failure",
                    "availability_status": "active",
                    "match_score": 66,
                    "previous_analysis": {"analysis_status": "complete", "match_score": 66},
                },
                {
                    "id": "old-1",
                    "company_id": "c-acme",
                    "company": "Acme",
                    "title": "Old C++ 岗位",
                    "detail_url": "https://catalog.example/old",
                    "jd_raw": "old jd",
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "availability_status": "active",
                },
            ],
            "analyses": [],
            "applications": [
                {"id": "application-1", "job_id": "existing-1", "stage": "applied", "note": "keep"}
            ],
            "read_only": True,
        },
    )
    return sources, checkpoints, hydration, catalog


def test_title_first_replay_buckets_rows_and_preserves_inputs(tmp_path, monkeypatch):
    calls = _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    catalog_before = catalog.read_bytes()
    output = tmp_path / "replay"

    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["policy_scope"] == "2027-autumn-three-industry-title-first"
    assert summary["network_calls"] == summary["model_calls"] == summary["database_writes"] == 0
    assert summary["score_calls"] == summary["imported_rows"] == 0
    assert summary["counts"]["list_rows_observed"] == 7
    assert summary["counts"]["title_screened_in"] == 5
    assert summary["counts"]["title_groups_after_dedupe"] == 4
    assert summary["counts"]["existing_skipped"] == 0
    assert summary["counts"]["existing_repair_total"] == 1
    assert summary["counts"]["existing_repair_failure"] == 1
    assert summary["counts"]["successful_jd_reuse"] == 1
    assert summary["counts"]["new_failure_placeholders"] == 1
    assert summary["counts"]["missing_jd_waiting"] == 1
    assert summary["counts"]["existing_detail_failures"] == 1
    assert summary["counts"]["unresolved_detail_failures"] == 2
    assert catalog.read_bytes() == catalog_before
    assert all(call.keys() == {"title"} for call in calls)

    observed = _read_jsonl(output / "observed-list-jobs.jsonl")
    assert len(observed) == 7
    assert {row["title"] for row in observed} >= {"实习 C++ 工程师", "博士 Agent 工程师"}

    existing = _read_jsonl(output / "existing-skipped.jsonl")
    assert existing == []

    repairs = _read_jsonl(output / "existing-repairs.jsonl")
    assert repairs[0]["disposition"] == "existing_repair_failure"
    assert repairs[0]["existing_job_ids"] == ["existing-1"]
    assert repairs[0]["repair_target_job"]["match_score"] == 66
    assert repairs[0]["repair_target_job"]["jd_raw"] == "catalog text"

    success = _read_jsonl(output / "successful-jd-reuse.jsonl")
    assert success[0]["capture_status"] == "complete"
    assert success[0]["proposed_job"]["jd_raw"] == "official robot JD"
    assert success[0]["proposed_job"]["cohort_status"] == "confirmed"
    assert success[0]["proposed_job"]["cohort_source"] == "offerbiu_user_policy"
    assert success[0]["proposed_job"]["original_cohort_evidence"]["cohort_status"] == "unknown"
    assert success[0]["score_action"] == "not_scored"

    failure = _read_jsonl(output / "new-failures.jsonl")
    assert failure[0]["capture_status"] == "failed"
    assert failure[0]["capture_failure_reason"] == "timeout"
    assert failure[0]["placeholder"]["match_score"] is None
    assert failure[0]["placeholder"]["detail_url"].endswith("agent-1")

    missing = _read_jsonl(output / "missing-jd.jsonl")
    assert missing[0]["capture_status"] == "pending"
    assert missing[0]["attempted"] is False
    assert missing[0]["failure_vs_unattempted"] == "unattempted"
    assert missing[0]["pending_job"]["cohort_status"] == "confirmed"

    companies = {row["company_id"]: row for row in _read_jsonl(output / "company-status.jsonl")}
    assert companies["c-acme"]["status"] == "partial"
    assert companies["c-acme"]["detail_failure_count"] == 1
    assert companies["c-acme"]["existing_detail_failure_count"] == 1
    assert companies["c-acme"]["unresolved_detail_failure_count"] == 2
    assert companies["c-acme"]["existing_repair_total"] == 1
    capture = companies["c-acme"]["sources"][0]["capture"]
    assert capture["raw_checkpoint"]["task_key"] == "acme-source"
    assert capture["raw_checkpoint"]["crawl"]["raw_jobs"]
    assert capture["pages_seen"] == 0
    assert capture["total_pages"] is None
    assert capture["advertised_total"] is None
    assert capture["source_runs"] == [{"termination_reason": "legacy_all_pages"}]
    assert "c-empty" not in companies
    assert summary["counts"]["companies"] == 1

    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert any(row["candidate_type"] == "detail_failure" for row in retry)
    assert any(row["candidate_type"] == "existing_detail_failure" for row in retry)
    assert any(row["failure_vs_unattempted"] == "unattempted" for row in retry)
    assert all(row["executed"] is False for row in retry)

    plan = json.loads((output / "deactivation-plan.json").read_text(encoding="utf-8"))
    assert plan["apply_allowed"] is False
    assert plan["counts"]["formal_writes_applied"] == 0
    assert any(row["action"] == "would_mark_inactive" for row in plan["plans"])

    applications = _read_jsonl(output / "applications-preserved.jsonl")
    assert applications == [{"id": "application-1", "job_id": "existing-1", "stage": "applied", "note": "keep"}]


def test_legacy_body_without_capture_metadata_is_healthy_and_pending_is_not_double_counted(
    tmp_path, monkeypatch
):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    existing = next(job for job in payload["jobs"] if job["id"] == "existing-1")
    existing.pop("capture_status")
    existing.pop("capture_failure_reason")
    _write_json(catalog, payload)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert module._existing_capture_state(existing) == "healthy"
    assert summary["counts"]["existing_skipped"] == 1
    assert summary["counts"]["existing_repair_total"] == 0
    assert summary["counts"]["existing_detail_pending"] == 0
    assert summary["counts"]["unresolved_detail_pending"] == 1
    assert summary["counts"]["title_groups_partition_valid"] is True
    company = _read_jsonl(output / "company-status.jsonl")[0]
    assert company["existing_unrediscovered_pending_count"] == 0
    assert company["missing_jd"] == 1
    assert company["detail_pending_count"] == 1
    assert company["unresolved_detail_pending_count"] == 1
    assert _read_jsonl(output / "existing-repairs.jsonl") == []
    skipped = _read_jsonl(output / "existing-skipped.jsonl")
    assert skipped[0]["existing_repair_required_ids"] == []
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert not any(row.get("candidate_type", "").startswith("existing_detail_") for row in retry)


def test_existing_empty_detail_pending_is_counted_once_and_keeps_old_job_fields(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    existing = next(job for job in payload["jobs"] if job["id"] == "existing-1")
    existing["jd_raw"] = ""
    existing.pop("capture_status")
    existing.pop("capture_failure_reason")
    rows = [row for row in _read_jsonl(hydration) if row.get("job", {}).get("title") != "C++ 工程师"]
    _write_json(catalog, payload)
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["existing_repair_pending"] == 1
    assert summary["counts"]["existing_repair_total"] == 1
    assert summary["counts"]["missing_jd_waiting"] == 1
    assert summary["counts"]["unresolved_detail_pending"] == 2
    company = _read_jsonl(output / "company-status.jsonl")[0]
    assert company["existing_detail_pending_count"] == 1
    assert company["detail_pending_count"] == 1
    assert company["missing_jd"] == 1
    assert company["unresolved_detail_pending_count"] == 2
    repair = _read_jsonl(output / "existing-repairs.jsonl")[0]
    assert repair["disposition"] == "existing_repair_pending"
    assert repair["existing_job_ids"] == ["existing-1"]
    assert repair["repair_target_job"]["jd_raw"] == ""
    assert repair["repair_target_job"]["id"] == "existing-1"
    new_successes = _read_jsonl(output / "successful-jd-reuse.jsonl")
    assert all(row["title"] != "C++ 工程师" for row in new_successes)
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert any(row["candidate_type"] == "existing_detail_missing" for row in retry)


def test_existing_repair_reuses_hydration_without_entering_new_success_bucket(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    rows = _read_jsonl(hydration)
    cpp = next(row for row in rows if row.get("job", {}).get("title") == "C++ 工程师")
    cpp.update(
        {
            "hydration_outcome": "hydrated",
            "request_made": True,
            "detail": "repaired catalog JD",
            "capture_evidence": _evidence(
                "repaired catalog JD",
                "https://acme.example/job/existing",
                "C++ 工程师",
            ),
        }
    )
    cpp.pop("error", None)
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["existing_repair_success"] == 1
    assert summary["counts"]["existing_repair_total"] == 1
    assert summary["counts"]["successful_jd_reuse"] == 1
    new_success = _read_jsonl(output / "successful-jd-reuse.jsonl")
    assert all(row["title"] != "C++ 工程师" for row in new_success)
    repair = _read_jsonl(output / "existing-repairs.jsonl")[0]
    assert repair["disposition"] == "existing_repair_success"
    assert repair["repaired_job"]["id"] == "existing-1"
    assert repair["repaired_job"]["match_score"] == 66
    assert repair["repaired_job"]["jd_raw"] == "repaired catalog JD"
    assert repair["repaired_job"]["capture_status"] == "complete"
    assert repair["score_action"] == "preserve"
    assert repair["needs_scoring"] is False
    assert repair["score_preserved"] is True
    assert repair["preserved_analysis"]["match_score"] == 66
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert not any(row.get("candidate_type") == "existing_detail_failure" for row in retry)
    assert _read_jsonl(output / "applications-preserved.jsonl") == [
        {"id": "application-1", "job_id": "existing-1", "stage": "applied", "note": "keep"}
    ]


def test_mixed_existing_duplicate_group_uses_healthy_representative_without_retry(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    cpp = next(job for job in payload["jobs"] if job["id"] == "existing-1")
    cpp.pop("capture_status")
    cpp.pop("capture_failure_reason")
    payload["jobs"].extend(
        [
            {
                "id": "robot-healthy",
                "company_id": "c-acme",
                "company": "Acme",
                "title": "机器人 软件工程",
                "detail_url": "https://catalog.example/robot-healthy",
                "jd_raw": "legacy robot JD",
                "capture_evidence": _evidence(
                    "legacy robot JD",
                    "https://catalog.example/robot-healthy",
                    "机器人 软件工程",
                ),
                "match_score": 44,
            },
            {
                "id": "robot-broken",
                "company_id": "c-acme",
                "company": "Acme",
                "title": "机器人 软件工程",
                "detail_url": "https://catalog.example/robot-broken",
                "jd_raw": "",
                "capture_status": "failed",
                "match_score": 12,
            },
        ]
    )
    _write_json(catalog, payload)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["existing_skipped"] == 2
    assert summary["counts"]["existing_repair_total"] == 0
    assert summary["counts"]["successful_jd_reuse"] == 0
    skipped = {row["title"]: row for row in _read_jsonl(output / "existing-skipped.jsonl")}
    assert skipped["机器人 软件工程"]["existing_job_ids"] == ["robot-healthy", "robot-broken"]
    assert skipped["机器人 软件工程"]["existing_repair_suppressed"] is True
    assert _read_jsonl(output / "existing-repairs.jsonl") == []
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert not any(row.get("title") == "机器人 软件工程" for row in retry)
    assert summary["counts"]["title_groups_partition_valid"] is True


def test_unrediscovered_existing_failure_stays_partial_without_retry_candidate(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    current = next(job for job in payload["jobs"] if job["id"] == "existing-1")
    current.pop("capture_status")
    current.pop("capture_failure_reason")
    unseen = next(job for job in payload["jobs"] if job["id"] == "old-1")
    unseen["title"] = "Unseen C++ 岗位"
    unseen["capture_status"] = "failed"
    _write_json(catalog, payload)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["existing_unrediscovered_failures"] == 1
    company = _read_jsonl(output / "company-status.jsonl")[0]
    assert company["existing_unrediscovered_failure_count"] == 1
    assert company["status"] == "partial"
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert not any(row.get("title") == "Unseen C++ 岗位" for row in retry)


def test_existing_repair_success_without_valid_score_is_marked_for_later_analysis(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    payload = json.loads(catalog.read_text(encoding="utf-8"))
    existing = next(job for job in payload["jobs"] if job["id"] == "existing-1")
    existing.pop("match_score")
    existing.pop("previous_analysis")
    rows = _read_jsonl(hydration)
    cpp = next(row for row in rows if row.get("job", {}).get("title") == "C++ 工程师")
    cpp.update(
        {
            "hydration_outcome": "hydrated",
            "request_made": True,
            "detail": "repaired catalog JD",
            "capture_evidence": _evidence(
                "repaired catalog JD",
                "https://acme.example/job/existing",
                "C++ 工程师",
            ),
        }
    )
    _write_json(catalog, payload)
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    repair = _read_jsonl(output / "existing-repairs.jsonl")[0]
    assert repair["score_action"] == "needs_scoring"
    assert repair["needs_scoring"] is True
    assert repair["score_preserved"] is False
    assert repair["repaired_job"]["match_score"] is None
    assert repair["preserved_analysis"] is None
    assert summary["score_calls"] == 0


def test_resume_rejects_old_v1_manifest_for_v2_replay(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    output = tmp_path / "replay"
    module.replay(sources, checkpoints, hydration, catalog, output)

    manifest_path = output / "replay-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "title-first-capture-replay.v1"
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="schema"):
        module.replay(sources, checkpoints, hydration, catalog, output, resume=True)


def test_pending_detail_attempt_is_not_reported_as_failure(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    rows = _read_jsonl(hydration)
    pending = next(row for row in rows if row.get("hydration_outcome") == "cohort_unconfirmed_not_hydrated")
    pending["hydration_outcome"] = "pending"
    pending["request_made"] = True
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["new_failure_placeholders"] == 1
    missing = _read_jsonl(output / "missing-jd.jsonl")
    llm = next(row for row in missing if row["title"] == "LLM 工程师")
    assert llm["attempted"] is True
    assert llm["failure_vs_unattempted"] == "pending"
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert any(row.get("candidate_type") == "detail_pending" for row in retry)


def test_tampered_body_and_unfinished_receipt_cannot_be_successful_reuse(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    rows = _read_jsonl(hydration)

    robot = next(row for row in rows if row.get("hydration_outcome") == "hydrated")
    robot["candidate_jd_raw"] = "tampered robot JD"
    robot["candidate_capture_evidence"] = _evidence(
        "official robot JD",
        "https://acme.example/job/robot-1",
        "机器人 软件工程",
    )

    agent = next(row for row in rows if row.get("hydration_outcome") == "fetch_failed")
    agent.update(
        {
            "hydration_outcome": "hydrated",
            "request_made": True,
            "candidate_jd_raw": "official agent JD",
            "candidate_capture_evidence": _evidence(
                "official agent JD",
                "https://acme.example/job/agent-1",
                "Agent 工程师",
            ),
        }
    )
    agent["candidate_capture_evidence"]["terminal_observed"] = False
    agent["candidate_capture_evidence"]["remaining_controls"] = ["load-more"]
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["successful_jd_reuse"] == 0
    assert summary["counts"]["new_failure_placeholders"] == 2
    failures = {row["title"]: row for row in _read_jsonl(output / "new-failures.jsonl")}
    assert failures["机器人 软件工程"]["capture_binding"]["chosen_evidence_origin"] == "candidate"
    assert failures["机器人 软件工程"]["capture_binding"]["assessment_reason_code"] == "capture_content_changed"
    assert failures["Agent 工程师"]["capture_binding"]["assessment_reason_code"] == "detail_not_terminated"
    assert failures["机器人 软件工程"]["placeholder"]["jd_raw"] is None
    assert failures["机器人 软件工程"]["placeholder"]["capture_evidence"] == {}
    assert failures["Agent 工程师"]["placeholder"]["jd_raw"] is None


def test_record_jd_raw_receipt_is_used_when_candidate_and_inline_detail_are_absent(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    rows = _read_jsonl(hydration)
    robot = next(row for row in rows if row.get("hydration_outcome") == "hydrated")
    robot.pop("detail")
    robot["jd_raw"] = "official robot JD"
    robot["capture_evidence"] = _evidence(
        "official robot JD",
        "https://acme.example/job/robot-1",
        "机器人 软件工程",
    )
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["successful_jd_reuse"] == 1
    success = _read_jsonl(output / "successful-jd-reuse.jsonl")[0]
    assert success["capture_binding"]["chosen_evidence_origin"] == "record"
    assert success["proposed_job"]["jd_raw"] == "official robot JD"


def test_explicit_empty_candidate_body_does_not_fall_back_to_inline_detail(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources, checkpoints, hydration, catalog = _base_inputs(tmp_path)
    rows = _read_jsonl(hydration)
    robot = next(row for row in rows if row.get("hydration_outcome") == "hydrated")
    robot["candidate_jd_raw"] = ""
    robot["candidate_capture_evidence"] = _evidence(
        "official robot JD",
        "https://acme.example/job/robot-1",
        "机器人 软件工程",
    )
    _write_jsonl(hydration, rows)

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["successful_jd_reuse"] == 0
    failures = {row["title"]: row for row in _read_jsonl(output / "new-failures.jsonl")}
    assert failures["机器人 软件工程"]["capture_binding"]["chosen_evidence_origin"] == "candidate"
    assert failures["机器人 软件工程"]["capture_binding"]["assessment_reason_code"] == "capture_source_missing"


def test_cli_profile_loader_uses_real_wrapper_and_passes_four_directions(tmp_path):
    profile_path = tmp_path / "candidate_profile.yaml"
    profile_path.write_text(
        """profile:
  skills:
    - C++
  direction: C++软件开发 / 机械臂与机器人开发 / 具身智能 / 大模型与Agent开发
  degree: 研究生
  job_type: 校招
  matching:
    direction_policy: parallel
    primary_directions:
      - C++ 软件开发 / Linux系统软件 / Qt客户端 / ROS机器人软件 / 测试开发
      - 机械臂与机器人开发 / 运动控制 / 运动规划 / 机器人视觉 / 系统集成
      - 具身智能 / 机械臂或人形机器人VLA / 模仿学习 / 强化学习
      - 大模型与Agent开发 / RAG / AI应用开发 / 模型部署训练
    secondary_directions: []
    project_evidence: []
    supporting_skills: []
    learning_targets: []
    unverified_skills: []
""",
        encoding="utf-8",
    )

    from packages.matching.models import Direction
    from packages.matching.rules import requested_directions
    from scripts import replay_title_first_capture as cli

    profile = cli._profile(profile_path)

    assert profile.__class__.__name__ == "CandidateProfile"
    assert set(requested_directions(profile)) == set(Direction)


def test_partial_list_blocks_historical_deactivation_and_resume_does_not_retest(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources = tmp_path / "sources.json"
    checkpoints = tmp_path / "checkpoints"
    hydration = tmp_path / "hydration.jsonl"
    catalog = tmp_path / "catalog.json"
    _write_json(
        sources,
        [{"key": "partial", "row": {"companyId": "c1", "companyName": "Partial", "applyUrl": "https://partial.example"}}],
    )
    checkpoints.mkdir()
    _checkpoint(
        checkpoints / "partial.json",
        "partial",
        "Partial",
        [{"title": "C++ 岗位", "detail_url": "https://partial.example/job/1"}],
        status="partial",
        pagination_complete=False,
        completeness_known=False,
    )
    hydration.write_text(
        json.dumps(
            {
                "company": "Partial",
                "job": {"title": "C++ 岗位", "detail_url": "https://partial.example/job/1"},
                "hydration_outcome": "fetch_failed",
                "request_made": True,
                "error": "blocked",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        catalog,
        {
            "companies": [{"id": "c1", "name": "Partial"}],
            "jobs": [
                {
                    "id": "old",
                    "company_id": "c1",
                    "title": "Old C++ 岗位",
                    "detail_url": "https://partial.example/old",
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                }
            ],
            "applications": [],
        },
    )
    output = tmp_path / "replay"
    first = module.replay(sources, checkpoints, hydration, catalog, output)
    second = module.replay(sources, checkpoints, hydration, catalog, output, resume=True)

    assert first["counts"]["new_failure_placeholders"] == 1
    assert second["counts"]["new_failure_placeholders"] == 1
    assert second["resume"]["retested"] == 0
    assert second["resume"]["failures_preserved"] is True
    plan = json.loads((output / "deactivation-plan.json").read_text(encoding="utf-8"))
    assert plan["plans"] == []
    assert plan["counts"]["blocked_companies"] == 1


def test_invalid_checkpoint_is_failed_not_unattempted(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources = tmp_path / "sources.json"
    checkpoints = tmp_path / "checkpoints"
    hydration = tmp_path / "hydration.jsonl"
    catalog = tmp_path / "catalog.json"
    _write_json(
        sources,
        [{"key": "broken", "row": {"companyId": "c1", "companyName": "Broken", "applyUrl": "https://broken.example"}}],
    )
    checkpoints.mkdir()
    (checkpoints / "broken.json").write_text("{not valid json", encoding="utf-8")
    hydration.write_text("", encoding="utf-8")
    _write_json(catalog, {"companies": [{"id": "c1", "name": "Broken"}], "jobs": [], "applications": []})

    output = tmp_path / "replay"
    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    company = _read_jsonl(output / "company-status.jsonl")[0]
    assert company["status"] == "failed"
    assert company["sources"][0]["capture"]["status"] == "failed"
    retry = _read_jsonl(output / "retry-candidates.jsonl")
    assert retry[0]["candidate_type"] == "source_capture_failed"
    assert retry[0]["failure_vs_unattempted"] == "failed"
    assert summary["counts"]["sources_with_checkpoints"] == 1


def test_same_title_is_company_scoped_and_url_native_id_do_not_dedupe(tmp_path, monkeypatch):
    _title_policy(monkeypatch)
    sources = tmp_path / "sources.json"
    checkpoints = tmp_path / "checkpoints"
    hydration = tmp_path / "hydration.jsonl"
    catalog = tmp_path / "catalog.json"
    _write_json(
        sources,
        [
            {"key": "one", "row": {"companyId": "one", "companyName": "One", "applyUrl": "https://one.example"}},
            {"key": "two", "row": {"companyId": "two", "companyName": "Two", "applyUrl": "https://two.example"}},
        ],
    )
    checkpoints.mkdir()
    _checkpoint(
        checkpoints / "one.json",
        "one",
        "One",
        [
            {"title": "C++ 工程师", "detail_url": "https://one.example/a", "native_job_id": "a"},
            {"title": " C++   工程师 ", "detail_url": "https://one.example/b", "native_job_id": "b"},
        ],
    )
    _checkpoint(
        checkpoints / "two.json",
        "two",
        "Two",
        [{"title": "C++ 工程师", "detail_url": "https://two.example/a", "native_job_id": "other"}],
    )
    _write_jsonl(
        hydration,
        [
            {
                "company": "One",
                "job": {"title": "C++ 工程师", "detail_url": "https://one.example/a"},
                "hydration_outcome": "hydrated",
                "detail": "one JD",
                "capture_evidence": _evidence("one JD", "https://one.example/a", "C++ 工程师"),
            }
        ],
    )
    _write_json(catalog, {"companies": [], "jobs": [], "applications": []})
    output = tmp_path / "replay"

    summary = module.replay(sources, checkpoints, hydration, catalog, output)

    assert summary["counts"]["list_rows_observed"] == 3
    assert summary["counts"]["title_groups_after_dedupe"] == 2
    assert summary["counts"]["successful_jd_reuse"] == 1
    assert summary["counts"]["missing_jd_waiting"] == 1
    filtered = _read_jsonl(output / "filtered-jobs.jsonl")
    assert {row["company_id"] for row in filtered} == {"one", "two"}
    assert all(row["title_key"] == "C++ 工程师" for row in filtered)
