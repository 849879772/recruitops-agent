from copy import deepcopy
import json

import pytest

from scripts import run_jd_repair_eval as evaluation


FULL_JD = "Responsibilities\n" + "Develop and maintain reliable systems. " * 8 + "\nRequirements\nPython experience."


def capture_evidence(detail, source_url):
    return {
        "status": "complete",
        "method": "test_fixture",
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": evaluation.sha256(detail.encode("utf-8")),
    }


def job(index, **changes):
    return {
        "company": "Example", "title": f"Engineer {index}", "jd_raw": "", "job_type": "campus",
        "jd_url": f"https://example.test/campus#/job/{index}", "link_kind": "detail",
        "cohort": 2027, "cohort_status": "confirmed", "cohort_source": evaluation.OC_LABEL,
        "cohort_evidence": "Frozen filtered OC snapshot", "campaign_scope": "trusted_source_override",
        "campaign_url": "https://example.test/campus", **changes,
    }


def source(jobs, company_id="company-a"):
    return {"company_id": company_id, "sha256": evaluation.digest(jobs), "source_index": 0,
            "run": {"jobs": jobs, "source_url": "https://example.test/campus",
                    "source_runs": [{"source_url": "https://example.test/campus", "observed_total": len(jobs)}]}}


def complete(sample=None, **changes):
    worker = {"detail": FULL_JD, "status": "complete", "source": "render",
              "attempts": ["moka_official:fetch_failed", "render:complete"], "error_type": "ProxyError",
              "identity_status": "matched", "identity_evidence": [f"title:{(sample or {}).get('title', 'Engineer')}"],
              **changes}
    worker.setdefault(
        "capture_evidence",
        capture_evidence(worker["detail"], (sample or {}).get("jd_url", "https://example.test/campus")),
    )
    return worker


def test_frozen_selection_excludes_completed_and_never_mutates_or_synthesizes_ids():
    rows = [job(i) for i in range(10)]
    rows[4]["id"] = "internal-hash"
    original = deepcopy(rows)
    report = {"results": [{"company_id": "company-a", "jd_results": [
        {"job_id": "pipeline-internal", "status": "complete", "detail_url": rows[0]["jd_url"]},
        {"job_id": "other-internal", "status": "not_sampled", "detail_url": rows[1]["jd_url"]},
    ]}]}
    result = evaluation.select_samples([source(rows)], [report])
    assert result == evaluation.select_samples([source(rows)], [report])
    assert rows == original
    chosen = [row for row in result["inventory"] if row["selection"] == "selected"]
    assert [row["raw_index"] for row in chosen] == list(range(1, 7))
    assert all("native_job_id" not in row["job"] for row in chosen)
    assert chosen[3]["job"]["id"] == "internal-hash"
    assert len(result["selected"]) == 6


@pytest.mark.parametrize("changes,reason", [
    ({"cohort": 2026}, "cohort_ineligible"),
    ({"cohort_status": "unconfirmed"}, "cohort_ineligible"),
    ({"cohort_source": "untrusted"}, "oc_source_unbound"),
    ({"campaign_url": "https://unrelated.test"}, "oc_source_unbound"),
    ({"title": "Intern engineer"}, "role_ineligible"),
    ({"link_kind": "list"}, "not_detail"),
    ({"jd_raw": FULL_JD}, "already_full"),
])
def test_selection_gates(changes, reason):
    result = evaluation.select_samples([source([job(0, **changes)])], [])
    assert result["inventory"][0]["selection"] == reason
    assert not result["selected"]


def test_missing_cohort_uses_bound_oc_rule_without_page_inspection(monkeypatch):
    raw = job(0)
    for key in ("cohort", "cohort_status", "cohort_source", "cohort_evidence", "campaign_scope"):
        raw.pop(key)
    raw["title"] = "2026 campus engineer"
    config = {"id": "company-a", "name": "Example", "discovery_source": "oc_snapshot",
              "oc_source_urls": ["https://example.test/campus"]}
    monkeypatch.setattr("packages.recruitment_core.job_cohorts.inspect_official_campaign",
                        lambda *a, **kw: pytest.fail("No list or campaign networking allowed"))
    missing = evaluation.select_samples([source([raw])], [])
    assert not missing["selected"]
    result = evaluation.select_samples([source([raw])], [], oc_configs=[config])
    hydrated = result["inventory"][0]["job"]
    assert (hydrated["cohort"], hydrated["cohort_status"]) == (2027, "confirmed")
    assert hydrated["campaign_scope"] == "trusted_source_override"
    assert "cohort_checked_at" not in hydrated
    assert result == evaluation.select_samples([source([raw])], [], oc_configs=[config])
    config["oc_source_urls"] = ["https://other.test"]
    assert not evaluation.select_samples([source([raw])], [], oc_configs=[config])["selected"]


def test_missing_cohort_can_reuse_persisted_oc_evidence():
    raw = job(0)
    raw.pop("cohort")
    result = evaluation.select_samples([source([raw])], [])
    assert result["inventory"][0]["job"]["cohort"] == 2027
    assert len(result["selected"]) == 1


def test_round_robin_company_balance_and_exclusions_are_company_scoped():
    rows = [job(i) for i in range(8)]
    report = {"results": [{"company_id": "company-a", "jd_results": [
        {"detail_url": rows[0]["jd_url"], "status": "complete"}]}]}
    result = evaluation.select_samples([source(rows), source(rows, "company-b")], [report])
    by_key = {row["eval_key"]: row for row in result["inventory"]}
    assert [by_key[key]["company_id"] for key in result["selected"]] == ["company-a", "company-b"] * 6
    summary = evaluation.summarize(result, [])
    assert summary["excluded_completed"] == 1
    assert summary["selected"] == 12


@pytest.mark.parametrize("changes,passed", [
    ({}, True),
    ({"identity_status": "request_bound", "identity_evidence": ["request_id:1"]}, True),
    ({"identity_status": "mismatch"}, False),
    ({"identity_status": "ambiguous"}, False),
    ({"identity_status": "unverified"}, False),
    ({"identity_evidence": []}, False),
    ({"status": "identity_mismatch"}, False),
    ({"detail": "short shell", "capture_evidence": {}}, False),
])
def test_identity_and_completeness_are_required(changes, passed):
    sample = evaluation.select_samples([source([job(0)])], [])["inventory"][0]
    result = evaluation.evaluate_result(sample, complete(**changes))
    assert (result["verdict"] == "passed") is passed
    assert result["attempts"] == ["moka_official:fetch_failed", "render:complete"]
    assert result["error_type"] == "ProxyError"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_budget_stop_is_not_failure_and_resume_never_retests_same_input(tmp_path):
    manifest = evaluation.select_samples([source([job(i) for i in range(4)])], [])
    clock, calls = Clock(), []

    def fetch(raw, *, timeout_seconds):
        calls.append(raw)
        assert timeout_seconds == 30
        clock.now += 45
        return complete(raw) if len(calls) == 1 else complete(raw, status="fetch_failed", detail="")

    report = evaluation.run_eval(manifest, tmp_path, budget_seconds=100, fetcher=fetch, monotonic=clock)
    assert len(calls) == 2
    assert report["summary"]["passed"] == report["summary"]["failed"] == 1
    assert report["summary"]["not_tested_selected"] == 2
    assert report["summary"]["completeness_rate_tested"] == 0.5
    assert report["session"]["stop_reason"] == "budget_exhausted"
    again = evaluation.run_eval(manifest, tmp_path, budget_seconds=100, fetcher=fetch, monotonic=clock)
    assert len(calls) == 2
    assert again["session"]["resume_skipped"] == 2
    assert again["session"]["new_calls"] == 0
    # A deliberate larger bounded budget continues ONLY untested records.
    final = evaluation.run_eval(manifest, tmp_path, budget_seconds=190, fetcher=fetch, monotonic=clock)
    assert len(calls) == 4
    assert final["summary"]["not_tested_selected"] == 0
    assert final["elapsed_seconds"] == 180


def test_zero_calls_under_insufficient_budget_and_timeout_is_actual_failure(tmp_path, capsys):
    manifest = evaluation.select_samples([source([job(0)])], [])

    def fetch(*a, **kw):
        raise evaluation.IsolatedOperationTimeout("SECRET-token cookie and private response")

    report = evaluation.run_eval(manifest, tmp_path, budget_seconds=1, fetcher=fetch)
    assert report["summary"]["failed"] == 0
    assert report["summary"]["completeness_rate_tested"] is None
    report = evaluation.run_eval(manifest, tmp_path, fetcher=fetch)
    assert report["summary"]["failed"] == 1
    assert report["results"][0]["status"] == "timeout"
    assert "SECRET" not in capsys.readouterr().out
    assert "SECRET" not in json.dumps(report)


def test_checkpoint_rejects_changed_input_and_preserves_results(tmp_path):
    manifest = evaluation.select_samples([source([job(0)])], [])
    evaluation.run_eval(manifest, tmp_path, fetcher=lambda *a, **kw: complete())
    old = (tmp_path / "report.json").read_bytes()
    manifest["inventory"][0]["job"]["title"] = "Changed"
    with pytest.raises(ValueError, match="different frozen inputs"):
        evaluation.run_eval(manifest, tmp_path, fetcher=lambda *a, **kw: pytest.fail("must not retry"))
    assert (tmp_path / "report.json").read_bytes() == old


def test_interrupted_slot_is_not_retried_or_counted_as_failure(tmp_path):
    manifest = evaluation.select_samples([source([job(0)])], [])
    key = manifest["selected"][0]
    evaluation.write_json(tmp_path / "checkpoint.json", {
        "inputs_sha256": evaluation.digest(manifest), "elapsed_seconds": 0,
        "in_flight": {"eval_key": key, "reserved_seconds": 45}, "interrupted": [], "sessions": [],
    })
    report = evaluation.run_eval(manifest, tmp_path, fetcher=lambda *a, **kw: pytest.fail("unknown prior call"))
    assert report["summary"]["tested"] == report["summary"]["failed"] == 0
    assert report["summary"]["not_tested_selected"] == 1
    assert report["interrupted"] == [key]
    assert report["elapsed_seconds"] >= 45


def test_frozen_artifacts_offline_replay_and_hash_tamper_guard(tmp_path, monkeypatch):
    raw_path = tmp_path / "raw.json"
    rows = [job(0), job(1)]
    evaluation.write_json(raw_path, {"company-a": source(rows)["run"]})
    prior_path = tmp_path / "prior.json"
    evaluation.write_json(prior_path, {"results": []})
    directory = tmp_path / "eval"
    manifest = evaluation.freeze_inputs([f"company-a={raw_path}"], [prior_path], directory, per_company=6)
    assert evaluation.load_frozen(directory) == manifest
    live = evaluation.run_eval(manifest, directory, fetcher=lambda *a, **kw: complete())
    monkeypatch.setattr(evaluation, "fetch_job_detail_result_isolated", lambda *a, **kw: pytest.fail("offline"))
    replayed = evaluation.replay(directory, tmp_path / "replay")
    assert replayed["summary"] == live["summary"]
    assert replayed["detail_calls"] == 0
    sample = manifest["inventory"][0]
    result_path = directory / "results" / f"{sample['eval_key']}.json"
    record = evaluation.read_json(result_path)
    assert record["worker"]["detail"] == FULL_JD
    record["worker"]["detail"] = "tampered"
    evaluation.write_json(result_path, record)
    with pytest.raises(ValueError, match="result hash"):
        evaluation.replay(directory, tmp_path / "replay")


@pytest.mark.parametrize("timeout,budget", [(46, 480), (45, 481), (0, 480)])
def test_budget_caps(tmp_path, timeout, budget):
    with pytest.raises(ValueError, match="Maximum"):
        evaluation.run_eval({}, tmp_path, timeout_seconds=timeout, budget_seconds=budget)
