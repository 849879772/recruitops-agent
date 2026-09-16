from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.discovery import consolidate_source_leads, filter_oc_snapshot
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, CompanySnapshot, JobSnapshot
from scripts.rebuild_oc_only_catalog import build_oc_only_catalog, prune_database
from scripts import oc_catalog_evaluation
from scripts.run_oc_full_crawl_eval import _lead_key


def _record(company: str, url: str) -> dict[str, str]:
    return {
        "company": company,
        "company_type": "民企",
        "recruitment_target": "2027届",
        "recruitment_type": "秋招",
        "industry": "机器人",
        "apply_url": url,
    }


def _write_snapshot(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text(
        json.dumps(
            {"captured_at": "2026-09-03T00:00:00Z", "records": records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_evaluation(path: Path, results: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


def test_catalog_contains_only_oc_companies_and_preserves_proven_adapter(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "2026-09-03T00:00:00Z",
                "records": [
                    _record("已接入公司", "https://known.jobs.feishu.cn/campus"),
                    _record("新公司", "https://app.mokahr.com/campus_apply/new/1"),
                    _record("待验证公司", "https://unknown.example/jobs"),
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evaluation = tmp_path / "eval.json"
    evaluation.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "source_url": "https://app.mokahr.com/campus_apply/new/1",
                        "crawler_key": "moka",
                        "integration_status": "connected_complete",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    current = [
        {
            "id": "known",
            "name": "已接入公司",
            "careers_url": "https://known.jobs.feishu.cn/campus",
            "crawler": "feishu",
            "integration_status": "connected",
        },
        {"id": "legacy", "name": "旧名单公司", "integration_status": "not_connected"},
    ]

    rows, summary = build_oc_only_catalog(snapshot, current, evaluation)

    assert {row["name"] for row in rows} == {"已接入公司", "新公司", "待验证公司"}
    assert all(row["discovery_source"] == "oc_snapshot" for row in rows)
    known = next(row for row in rows if row["name"] == "已接入公司")
    assert known["id"] == "known"
    assert known["careers_url"] == "https://known.jobs.feishu.cn/campus"
    assert known["crawler"] == "feishu"
    assert known["integration_status"] == "not_connected"
    new = next(row for row in rows if row["name"] == "新公司")
    assert new["crawler"] == "moka"
    assert new["integration_status"] == "connected"
    pending = next(row for row in rows if row["name"] == "待验证公司")
    assert pending["integration_status"] == "not_connected"
    assert summary["companies"] == 3


def test_existing_company_upgrades_from_best_latest_lead_evaluation(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    first_url = "https://upgrade.example/campus-a"
    second_url = "https://upgrade.example/campus-b"
    _write_snapshot(
        snapshot,
        [_record("升级公司", first_url), _record("升级公司", second_url)],
    )
    evaluation = tmp_path / "eval.json"
    _write_evaluation(
        evaluation,
        [
            {
                "source_url": first_url,
                "crawler_key": "render",
                "integration_status": "connected_complete",
                "completed_at": "2026-09-03T01:00:00Z",
            },
            {
                "source_url": first_url,
                "crawler_key": "render",
                "integration_status": "invalid_entry",
                "error_code": "stale_entry",
                "completed_at": "2026-09-03T03:00:00Z",
            },
            {
                "source_url": second_url,
                "crawler_key": "moka",
                "integration_status": "connected_complete",
                "completed_at": "2026-09-03T02:00:00Z",
            },
        ],
    )
    current = [
        {
            "id": "upgrade",
            "name": "升级公司",
            "careers_url": first_url,
            "crawler": "render",
            "integration_status": "not_connected",
            "integration_note": "old failure",
            "organization_id": "org-existing",
        }
    ]

    rows, _ = build_oc_only_catalog(snapshot, current, evaluation)

    assert len(rows) == 1
    assert rows[0]["integration_status"] == "connected"
    assert "integration_note" not in rows[0]
    assert rows[0]["careers_url"] == second_url
    assert rows[0]["crawler"] == "moka"
    assert rows[0]["organization_id"] == "org-existing"


def test_existing_company_downgrades_from_latest_evaluation(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    url = "https://downgrade.example/campus"
    _write_snapshot(snapshot, [_record("降级公司", url)])
    evaluation = tmp_path / "eval.json"
    _write_evaluation(
        evaluation,
        [
            {
                "source_url": url,
                "integration_status": "connected_complete",
                "completed_at": "2026-09-03T01:00:00Z",
            },
            {
                "source_url": url,
                "integration_status": "needs_adapter",
                "error_code": "site_adapter_required",
                "completed_at": "2026-09-03T02:00:00Z",
            },
        ],
    )
    current = [
        {
            "id": "downgrade",
            "name": "降级公司",
            "careers_url": url,
            "crawler": "render",
            "integration_status": "connected",
        }
    ]

    rows, _ = build_oc_only_catalog(snapshot, current, evaluation)

    assert rows[0]["integration_status"] == "not_connected"
    assert rows[0]["integration_note"] == "OC-only catalog: site_adapter_required"


def test_existing_company_without_evaluation_fails_closed(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    url = "https://unevaluated.example/campus"
    _write_snapshot(snapshot, [_record("未评估公司", url)])
    current = [
        {
            "id": "unevaluated",
            "name": "未评估公司",
            "careers_url": url,
            "crawler": "render",
            "integration_status": "connected",
            "integration_note": "manual acceptance retained",
        }
    ]

    rows, _ = build_oc_only_catalog(snapshot, current, tmp_path / "missing.json")

    assert rows[0]["integration_status"] == "not_connected"
    assert rows[0]["integration_note"] == (
        "OC-only catalog: no_complete_crawler_acceptance_evidence"
    )
    assert rows[0]["oc_coverage_status"] == "no_evidence"
    assert rows[0]["oc_source_coverage"][0]["evidence_status"] == "no_evidence"


def _result(url: str, *, crawler: str = "moka", status: str = "connected_complete", **extra):
    return {"source_url": url, "crawler_key": crawler, "integration_status": status, **extra}


def _write_report(path: Path, results: list[dict], completed_at: str, **metadata) -> None:
    path.write_text(json.dumps({
        "completed_at": completed_at,
        "snapshot_captured_at": "2026-09-03T00:00:00Z",
        **metadata,
        "results": results,
    }), encoding="utf-8")


def test_same_url_acceptance_switches_stale_crawler_without_mutating_inputs(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    url = "https://same.example/campus"
    _write_snapshot(snapshot, [_record("Same", url)])
    _write_evaluation(evaluation, [_result(url, crawler="beisen")])
    current = [{"id": "same", "name": "Same", "careers_url": url, "crawler": "render",
                "integration_status": "connected", "custom_options": {"preserve": True}}]
    before = copy.deepcopy(current)
    before_snapshot = snapshot.read_bytes()

    rows, _ = build_oc_only_catalog(snapshot, current, evaluation)

    assert rows[0]["careers_url"] == url
    assert rows[0]["crawler"] == "beisen"
    assert rows[0]["oc_all_sources_complete"] is True
    assert current == before
    assert snapshot.read_bytes() == before_snapshot


@pytest.mark.parametrize("binding", ["original_source_url", "lead_key"])
def test_discovered_alias_matches_original_and_updates_canonical_url(tmp_path: Path, binding: str) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    original, canonical = "https://alias.example/entry", "https://alias.jobs.feishu.cn/campus"
    _write_snapshot(snapshot, [_record("Alias", original)])
    lead = consolidate_source_leads(filter_oc_snapshot(snapshot).leads)[0]
    evidence = {binding: original if binding == "original_source_url" else _lead_key(lead)}
    _write_evaluation(evaluation, [
        _result(original, status="needs_adapter", completed_at="2026-09-03T01:00:00Z"),
        _result(canonical, crawler="feishu", discovered_entry_url=canonical,
                completed_at="2026-09-03T02:00:00Z", **evidence),
    ])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    assert rows[0]["integration_status"] == "connected"
    assert rows[0]["careers_url"] == canonical
    assert rows[0]["crawler"] == "feishu"
    coverage = rows[0]["oc_source_coverage"][0]
    assert coverage["source_url"] == original
    assert set(coverage["source_urls"]) == {original, canonical}
    assert rows[0]["oc_canonical_urls"] == [canonical]
    if binding == "lead_key":
        assert coverage["lead_key"] == _lead_key(lead)


def test_effective_source_alias_matches_and_promotes_crawl_source(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    original = "https://alias.example/entry"
    discovered = "https://alias.example/careers"
    effective = "https://alias.example/careers?page=1"
    _write_snapshot(snapshot, [_record("Alias", original)])
    _write_evaluation(evaluation, [{
        "lead_key": _lead_key(consolidate_source_leads(filter_oc_snapshot(snapshot).leads)[0]),
        "crawler_key": "render",
        "integration_status": "connected_complete",
        "source_url": original,
        "discovered_entry_url": discovered,
        "crawl_source_url": discovered,
        "effective_source_urls": [effective],
        "source_runs": [{"source_url": original, "effective_source_url": effective}],
    }])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    assert rows[0]["integration_status"] == "connected"
    assert rows[0]["careers_url"] == discovered
    assert effective in rows[0]["oc_source_coverage"][0]["source_urls"]


def test_newer_alias_only_retry_overrides_original_url_evidence(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    original, canonical = "https://alias.example/entry", "https://alias.example/campus"
    _write_snapshot(snapshot, [_record("Alias", original)])
    _write_evaluation(evaluation, [
        _result(original, discovered_entry_url=canonical, completed_at="2026-09-03T01:00:00Z"),
        _result(canonical, status="needs_adapter", error_code="timeout",
                completed_at="2026-09-03T02:00:00Z"),
    ])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    assert rows[0]["integration_status"] == "not_connected"
    assert rows[0]["oc_source_coverage"][0]["error_code"] == "timeout"


def test_default_selects_latest_full_and_matching_newer_partial_only(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "discovery" / "oc.json"
    snapshot.parent.mkdir()
    evaluations = tmp_path / "evals"
    evaluations.mkdir()
    first, second = "https://first.example/campus", "https://second.example/campus"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    old, baseline = evaluations / "old.json", evaluations / "baseline.json"
    old_partial, partial = evaluations / "old_partial.json", evaluations / "newest_test.json"
    _write_report(old, [_result(first, crawler="old"), _result(second)], "2026-09-03T01:00:00Z")
    _write_report(old_partial, [_result(first, status="needs_adapter")], "2026-09-03T02:00:00Z")
    _write_report(baseline, [_result(first), _result(second, status="needs_adapter")], "2026-09-03T03:00:00Z")
    _write_report(partial, [_result(second, crawler="feishu")], "2026-09-03T04:00:00Z")
    _write_report(evaluations / "other_snapshot.json", [_result(first, status="needs_adapter")],
                  "2026-09-03T05:00:00Z", snapshot_captured_at="2026-09-02T00:00:00Z")
    _write_report(evaluations / "wrong_baseline.json", [_result(first, status="needs_adapter")],
                  "2026-09-03T06:00:00Z", scope="targeted", baseline=str(old))
    loaded = []
    read = oc_catalog_evaluation._read_evaluation

    def counted_read(path):
        loaded.append(path)
        return read(path)

    monkeypatch.setattr(oc_catalog_evaluation, "_read_evaluation", counted_read)
    rows, summary = build_oc_only_catalog(snapshot, [])

    by_name = {row["name"]: row for row in rows}
    assert by_name["First"]["crawler"] == "moka"
    assert by_name["First"]["integration_status"] == "connected"
    assert by_name["Second"]["crawler"] == "feishu"
    selection = summary["evaluation"]
    assert selection["baseline"] == str(baseline)
    assert selection["overlays"] == [str(partial)]
    assert selection["baseline_results"] == 2
    assert selection["overlay_results"] == 1
    assert old not in loaded and old_partial not in loaded
    assert evaluations / "other_snapshot.json" not in loaded


@pytest.mark.parametrize("correct_hash", [True, False])
def test_targeted_overlay_requires_matching_hash_and_baseline(tmp_path: Path, correct_hash: bool) -> None:
    snapshot, evaluations = tmp_path / "oc.json", tmp_path / "evals"
    evaluations.mkdir()
    url = "https://targeted.example/campus"
    _write_snapshot(snapshot, [_record("Targeted", url)])
    baseline, partial = evaluations / "full.json", evaluations / "targeted.json"
    _write_report(baseline, [_result(url, crawler="render")], "2026-09-03T01:00:00Z")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest() if correct_hash else "0" * 64
    _write_report(partial, [_result(url, crawler="beisen", original_source_url=url)],
                  "2026-09-03T02:00:00Z", scope="targeted", baseline=baseline.name,
                  snapshot_captured_at=None, snapshot_sha256=digest)

    rows, summary = build_oc_only_catalog(snapshot, [], evaluation_dir=evaluations)

    assert summary["evaluation"]["baseline"] == str(baseline)
    assert summary["evaluation"]["overlays"] == ([str(partial)] if correct_hash else [])
    assert rows[0]["crawler"] == ("beisen" if correct_hash else "render")


def test_partial_without_full_baseline_is_not_promoted(tmp_path: Path) -> None:
    snapshot, evaluations = tmp_path / "oc.json", tmp_path / "evals"
    evaluations.mkdir()
    first, second = "https://first.example/campus", "https://second.example/campus"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    _write_report(evaluations / "oc_full_crawl_latest.json", [_result(first)],
                  "2026-09-03T01:00:00Z", summary={"company_count": 2}, scope="full")

    rows, summary = build_oc_only_catalog(snapshot, [], evaluation_dir=evaluations)

    assert summary["evaluation"]["baseline"] is None
    assert all(row["oc_coverage_status"] == "no_evidence" for row in rows)


def test_multi_project_mixed_result_retains_source_failures_and_missing_evidence(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    urls = [f"https://acme.jobs.feishu.cn/campus?project={value}" for value in ("a", "b", "c")]
    projects = ["Acme - Alpha program", "Acme - Beta program", "Acme - Gamma program"]
    _write_snapshot(snapshot, [_record(name, url) for name, url in zip(projects, urls)])
    current = [{"id": "acme", "name": "Acme", "careers_url": urls[0], "crawler": "render"}]
    _write_evaluation(evaluation, [
        _result(urls[0], crawler="feishu", source_projects=projects),
        _result(urls[1], status="connected_partial", error_code="pagination_incomplete"),
    ])

    rows, summary = build_oc_only_catalog(snapshot, current, evaluation)

    assert len(rows) == 1
    assert rows[0]["integration_status"] == "connected"
    assert rows[0]["oc_coverage_status"] == "partial"
    assert rows[0]["oc_all_sources_complete"] is False
    coverage = {item["project_name"]: item for item in rows[0]["oc_source_coverage"]}
    assert coverage[projects[0]]["evidence_status"] == "accepted"
    assert coverage[projects[1]]["evidence_status"] == "known_failure"
    assert coverage[projects[1]]["error_code"] == "pagination_incomplete"
    assert coverage[projects[2]]["evidence_status"] == "no_evidence"
    assert set(rows[0]["oc_canonical_urls"]) == set(urls)
    assert summary["partial_source_coverage"] == 1


def test_unmatched_projects_on_one_tenant_remain_separate_without_parent_evidence(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    urls = [
        "https://same.jobs.feishu.cn/campus/?project=alpha",
        "https://same.jobs.feishu.cn/campus/?project=beta",
    ]
    _write_snapshot(snapshot, [
        _record("Same Company", urls[0]),
        _record("Same Company - Beta Project", urls[1]),
    ])
    _write_evaluation(evaluation, [_result(urls[0]), _result(urls[1])])

    rows, summary = build_oc_only_catalog(snapshot, [], evaluation)

    assert len(rows) == 2
    assert summary["consolidated_entries"] == 1
    assert {row["careers_url"] for row in rows} == set(urls)


def test_shared_tenant_does_not_merge_distinct_existing_business_units(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    urls = ["https://app.mokahr.com/campus_apply/acme/1", "https://app.mokahr.com/campus_apply/acme/2"]
    names = ["Acme Robotics", "Acme Games"]
    _write_snapshot(snapshot, [_record(name, url) for name, url in zip(names, urls)])
    _write_evaluation(evaluation, [_result(urls[0])])
    current = [{"id": name, "name": name, "careers_url": url, "crawler": "render"}
               for name, url in zip(names, urls)]

    rows, _ = build_oc_only_catalog(snapshot, current, evaluation)
    fresh_rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    assert {row["id"] for row in rows} == set(names)
    assert len(fresh_rows) == 2
    assert len({row["id"] for row in fresh_rows}) == 2
    assert next(row for row in rows if row["name"] == names[1])["oc_coverage_status"] == "no_evidence"


def test_shared_tenant_does_not_absorb_new_company_into_single_known_company(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    robotics = "https://app.mokahr.com/campus_apply/acme/1"
    games = "https://app.mokahr.com/campus_apply/acme/2"
    _write_snapshot(snapshot, [_record("Acme Robotics", robotics), _record("Acme Games", games)])
    _write_evaluation(evaluation, [_result(robotics)])
    current = [{"id": "robotics", "name": "Acme Robotics", "careers_url": robotics}]

    rows, summary = build_oc_only_catalog(snapshot, current, evaluation)

    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"Acme Robotics", "Acme Games"}
    assert by_name["Acme Robotics"]["id"] == "robotics"
    assert by_name["Acme Robotics"]["oc_source_urls"] == [robotics]
    assert by_name["Acme Games"]["oc_source_urls"] == [games]
    assert by_name["Acme Games"]["oc_coverage_status"] == "no_evidence"
    assert summary["companies"] == 2
    assert summary["consolidated_entries"] == 1
    assert summary["preserved_existing"] == summary["new_or_ambiguous"] == 1


@pytest.mark.parametrize("configured", [False, True])
def test_company_without_any_address_is_retained(tmp_path: Path, configured: bool) -> None:
    snapshot = tmp_path / "oc.json"
    _write_snapshot(snapshot, [_record("No Address", "")])
    current = [{"id": "no-address", "name": "No Address"}] if configured else []

    rows, summary = build_oc_only_catalog(snapshot, current)

    assert len(rows) == summary["companies"] == 1
    assert rows[0]["name"] == "No Address"
    assert rows[0]["careers_url"] == ""
    assert rows[0]["oc_source_projects"] == ["No Address"]
    assert rows[0]["oc_source_urls"] == []
    assert len(rows[0]["oc_source_coverage"]) == 1
    assert rows[0]["oc_source_coverage"][0]["source_url"] == ""
    assert rows[0]["oc_coverage_status"] == "no_evidence"
    if configured:
        assert rows[0]["id"] == "no-address"


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("other_url", [
    "https://app.mokahr.com/campus_apply/acme/2", "https://acme.jobs.feishu.cn/campus",
])
def test_same_name_multiple_entries_have_one_entity_and_unique_source_coverage(
    tmp_path: Path, configured: bool, other_url: str,
) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    first = "https://app.mokahr.com/campus_apply/acme/1"
    _write_snapshot(snapshot, [
        _record("Acme", first), _record("Acme", other_url), _record("Acme", first),
    ])
    _write_evaluation(evaluation, [_result(first)])
    current = [{"id": "acme", "name": "Acme", "careers_url": first}] if configured else []

    rows, summary = build_oc_only_catalog(snapshot, current, evaluation)

    assert len(rows) == 1
    assert set(rows[0]["oc_source_urls"]) == {first, other_url}
    coverage = rows[0]["oc_source_coverage"]
    assert len(coverage) == 2
    assert {item["source_url"] for item in coverage} == {first, other_url}
    assert rows[0]["oc_coverage_status"] == "partial"
    assert summary["partial_source_coverage"] == 1
    repeated, _ = build_oc_only_catalog(snapshot, rows, evaluation)
    assert repeated[0]["id"] == rows[0]["id"]
    assert len(repeated[0]["oc_source_coverage"]) == 2


def test_explicit_alias_and_project_parent_merge_without_tenant_assumptions(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    names = ["Acme Robotics", "Acme Robots", "Acme Robotics - Alpha program", "Acme Games"]
    urls = [f"https://app.mokahr.com/campus_apply/acme/{i}" for i in range(1, 5)]
    _write_snapshot(snapshot, [_record(name, url) for name, url in zip(names, urls)])
    current = [
        {"id": "robotics", "name": names[0], "aliases": [names[1]], "careers_url": urls[0]},
        {"id": "games", "name": names[3], "careers_url": urls[3]},
    ]

    rows, _ = build_oc_only_catalog(snapshot, current)

    assert len(rows) == 2
    robotics = next(row for row in rows if row["id"] == "robotics")
    assert set(robotics["oc_source_projects"]) == set(names[:3])
    assert set(robotics["oc_source_urls"]) == set(urls[:3])
    assert len(robotics["oc_source_coverage"]) == 3
    assert next(row for row in rows if row["id"] == "games")["oc_source_urls"] == [urls[3]]


def test_ambiguous_alias_does_not_choose_a_parent_by_shared_tenant(tmp_path: Path) -> None:
    snapshot = tmp_path / "oc.json"
    url = "https://app.mokahr.com/campus_apply/acme/1"
    _write_snapshot(snapshot, [_record("Shared Alias", url)])
    current = [
        {"id": "one", "name": "One", "aliases": ["Shared Alias"], "careers_url": url},
        {"id": "two", "name": "Two", "aliases": ["Shared Alias"]},
    ]

    rows, summary = build_oc_only_catalog(snapshot, current)

    assert len(rows) == 1
    assert rows[0]["name"] == "Shared Alias"
    assert rows[0]["id"] not in {"one", "two"}
    assert summary["preserved_existing"] == 0


@pytest.mark.parametrize("alias_field", ["discovered_entry_url", "effective_source_urls"])
def test_effective_url_does_not_transfer_success_to_another_source(tmp_path: Path, alias_field: str) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    first, second = "https://first.example/entry", "https://second.example/entry"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    alias = second if alias_field == "discovered_entry_url" else [second]
    _write_evaluation(evaluation, [_result(first, **{alias_field: alias})])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    by_name = {row["name"]: row for row in rows}
    assert by_name["First"]["oc_coverage_status"] == "complete"
    assert by_name["Second"]["oc_coverage_status"] == "no_evidence"


def test_url_chain_does_not_launder_cross_company_success(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    first, second = "https://first.example/entry", "https://second.example/entry"
    shared, final = "https://shared.example/campus", "https://final.example/campus"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    _write_evaluation(evaluation, [
        _result(first, company="First", discovered_entry_url=shared, status="needs_adapter",
                completed_at="2026-09-03T01:00:00Z"),
        _result(shared, company="Second", discovered_entry_url=final, status="needs_adapter",
                completed_at="2026-09-03T02:00:00Z"),
        _result(final, completed_at="2026-09-03T03:00:00Z"),
    ])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    by_name = {row["name"]: row for row in rows}
    assert by_name["First"]["oc_coverage_status"] == "failed"
    assert by_name["Second"]["oc_coverage_status"] == "no_evidence"


def test_shared_redirect_retry_without_company_scope_is_ambiguous(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    first, second, shared = "https://first.example/entry", "https://second.example/entry", "https://shared.example/jobs"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    _write_evaluation(evaluation, [
        _result(first, company="First", discovered_entry_url=shared, status="needs_adapter",
                completed_at="2026-09-03T01:00:00Z"),
        _result(second, company="Second", discovered_entry_url=shared, status="needs_adapter",
                completed_at="2026-09-03T02:00:00Z"),
        _result(shared, completed_at="2026-09-03T03:00:00Z"),
    ])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    assert all(row["oc_coverage_status"] == "failed" for row in rows)


def test_keyed_tenant_result_covers_only_its_original_project_url(tmp_path: Path) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    first, second = [f"https://app.mokahr.com/campus_apply/acme/{i}" for i in (1, 2)]
    _write_snapshot(snapshot, [_record("Acme Robotics", first), _record("Acme Games", second)])
    lead = consolidate_source_leads(filter_oc_snapshot(snapshot).leads)[0]
    _write_evaluation(evaluation, [
        _result(first, lead_key=_lead_key(lead), effective_source_urls=[second]),
    ])

    rows, _ = build_oc_only_catalog(snapshot, [], evaluation)

    by_name = {row["name"]: row for row in rows}
    assert by_name["Acme Robotics"]["oc_coverage_status"] == "complete"
    assert by_name["Acme Games"]["oc_coverage_status"] == "no_evidence"


def test_persisted_proof_survives_unrelated_partial_omission(tmp_path: Path) -> None:
    snapshot, baseline, partial = tmp_path / "oc.json", tmp_path / "baseline.json", tmp_path / "partial.json"
    first, second = "https://first.example/campus", "https://second.example/campus"
    _write_snapshot(snapshot, [_record("First", first), _record("Second", second)])
    _write_evaluation(baseline, [_result(first), _result(second)])
    proven, _ = build_oc_only_catalog(snapshot, [], baseline)
    _write_evaluation(partial, [_result(second, status="needs_adapter", error_code="timeout")])
    before = copy.deepcopy(proven)

    rows, _ = build_oc_only_catalog(snapshot, proven, partial)

    assert proven == before
    first_row = next(row for row in rows if row["name"] == "First")
    assert first_row["id"] == next(row for row in proven if row["name"] == "First")["id"]
    assert first_row["integration_status"] == "connected"
    assert first_row["oc_source_coverage"][0]["preserved"] is True
    assert next(row for row in rows if row["name"] == "Second")["integration_status"] == "not_connected"


def test_evaluation_loading_limits_and_invalid_explicit_artifact(tmp_path: Path, monkeypatch) -> None:
    snapshot, evaluation = tmp_path / "oc.json", tmp_path / "eval.json"
    url = "https://bounded.example/campus"
    _write_snapshot(snapshot, [_record("Bounded", url)])
    _write_evaluation(evaluation, [_result(url)])
    monkeypatch.setattr(oc_catalog_evaluation, "MAX_EVALUATION_BYTES", 10)

    with pytest.raises(ValueError, match="byte limit"):
        build_oc_only_catalog(snapshot, [], evaluation)


def test_preview_cli_never_writes_config_or_database(tmp_path: Path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace
    from scripts import rebuild_oc_only_catalog as catalog

    snapshot, companies = tmp_path / "oc.json", tmp_path / "companies.yaml"
    _write_snapshot(snapshot, [_record("Preview", "https://preview.example/campus")])
    companies.write_text("companies: []\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "get_settings", lambda: SimpleNamespace(
        oc_snapshot_file=snapshot, companies_config=companies, agent_root=tmp_path,
    ))
    monkeypatch.setattr(catalog.sys, "argv", ["rebuild_oc_only_catalog.py"])

    def forbidden(*args, **kwargs):
        pytest.fail("read-only preview attempted a write")

    monkeypatch.setattr(catalog, "write_catalog", forbidden)
    monkeypatch.setattr(catalog, "prune_database", forbidden)

    assert catalog.main() == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert companies.read_text(encoding="utf-8") == "companies: []\n"


def test_prune_preserves_application_linked_job(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'agent.db'}"
    storage = Storage.from_url(database_url, initialize=True)
    with storage.transaction(write=True) as session:
        session.add_all(
            [
                CompanySnapshot(
                    id="oc-company", name="OC", aliases=[], integration_status="connected",
                    source="test", source_ref="oc",
                ),
                CompanySnapshot(
                    id="legacy-company", name="Legacy", aliases=[], integration_status="connected",
                    source="test", source_ref="legacy",
                ),
                JobSnapshot(
                    id="oc-job", company_id="oc-company", title="A", detail_url="https://oc/job",
                    cohort_status="confirmed", batch="formal", source="test", source_ref="oc-job",
                ),
                JobSnapshot(
                    id="protected-job", company_id="legacy-company", title="B", detail_url="https://old/job/1",
                    cohort_status="confirmed", batch="formal", source="test", source_ref="protected-job",
                ),
                JobSnapshot(
                    id="discard-job", company_id="legacy-company", title="C", detail_url="https://old/job/2",
                    cohort_status="confirmed", batch="formal", source="test", source_ref="discard-job",
                ),
                ApplicationSnapshot(
                    id="application-1", company_name="Legacy", job_title="B", job_id="protected-job",
                    stage="applied", idempotency_key="application-1", stage_history=[],
                    source="test", source_ref="application-1",
                ),
            ]
        )

    result = prune_database(
        database_url,
        [
            {
                "id": "oc-company", "name": "OC", "aliases": [],
                "careers_url": "https://oc/jobs", "crawler": "render",
                "integration_status": "connected",
            }
        ],
    )

    with storage.session() as session:
        assert set(session.scalars(select(CompanySnapshot.id))) == {"oc-company"}
        assert set(session.scalars(select(JobSnapshot.id))) == {"oc-job", "protected-job"}
    assert result["jobs_deleted"] == 1
    assert result["application_jobs_preserved"] == 1


def test_replace_catalog_deletes_all_jobs_but_keeps_application_unchanged(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'agent.db'}"
    storage = Storage.from_url(database_url, initialize=True)
    with storage.transaction(write=True) as session:
        session.add_all(
            [
                CompanySnapshot(
                    id="legacy-company", name="Legacy", aliases=[],
                    integration_status="connected", source="test", source_ref="legacy",
                ),
                JobSnapshot(
                    id="legacy-job", company_id="legacy-company", title="Legacy job",
                    detail_url="https://old/job", cohort_status="confirmed", batch="formal",
                    source="test", source_ref="legacy-job",
                ),
                ApplicationSnapshot(
                    id="application-1", company_name="Legacy", job_title="Legacy job",
                    job_id="legacy-job", stage="assessment", idempotency_key="application-1",
                    stage_history=[{"stage": "applied"}], source="test",
                    source_ref="application-1",
                ),
            ]
        )

    result = prune_database(
        database_url,
        [
            {
                "id": "oc-company", "name": "OC", "aliases": [],
                "careers_url": "https://oc/jobs", "crawler": "render",
                "integration_status": "connected",
            }
        ],
        replace_catalog=True,
    )

    with storage.session() as session:
        assert set(session.scalars(select(CompanySnapshot.id))) == {"oc-company"}
        assert list(session.scalars(select(JobSnapshot.id))) == []
        application = session.get(ApplicationSnapshot, "application-1")
        assert application is not None
        assert application.company_name == "Legacy"
        assert application.job_title == "Legacy job"
        assert application.job_id == "legacy-job"
        assert application.stage == "assessment"
        assert application.stage_history == [{"stage": "applied"}]
    assert result["jobs_deleted"] == 1
    assert result["applications_before"] == 1
    assert result["applications_after"] == 1
    assert result["application_jobs_preserved"] == 0
    assert result["application_job_refs_retained"] == 1
