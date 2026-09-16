from __future__ import annotations

import copy
import json

from packages.discovery import (
    filter_oc_snapshot,
    is_allowed_recruitment_type,
    is_eligible_record,
    matches_target_industry,
)
from packages.discovery.oc_snapshot import OC_FILTER_VERSION


def _valid(**overrides):
    record = {
        "company": "甲公司",
        "company_type": "民企",
        "recruitment_target": "2027届应届生",
        "recruitment_type": "秋招",
        "industry": "软件技术",
        "apply_url": "https://jia.example/jobs",
    }
    record.update(overrides)
    return record


def test_oc_rules_are_exact_and_compositional() -> None:
    base = _valid(
        recruitment_target="2027届,海外往届",
        recruitment_type="秋招提前批/秋招",
        industry="智能硬件/机器人",
    )
    assert is_allowed_recruitment_type(base)
    assert matches_target_industry(base)
    assert is_eligible_record(base)
    assert is_allowed_recruitment_type({**base, "recruitment_type": "秋招,实习"})
    assert is_allowed_recruitment_type({**base, "recruitment_type": "春招/秋招"})
    assert not is_allowed_recruitment_type({**base, "recruitment_type": "春招/实习"})
    assert not is_eligible_record({**base, "company_type": "外企/合资"})
    assert not is_eligible_record({**base, "recruitment_target": "2026届"})
    assert not matches_target_industry({**base, "industry": "汽车零部件"})


def test_filter_oc_snapshot_groups_deterministically_and_does_not_mutate_input() -> None:
    snapshot = {
        "source_url": "https://www.givemeoc.com/",
        "captured_at": "2026-08-19T12:10:42Z",
        "pagination": {"total_pages": 3},
        "filters": {"company_types": ["民企"]},
        "records": [
            _valid(company="乙科技", industry="人工智能", apply_url="https://b.example/jobs"),
            _valid(company="甲公司", apply_url="https://jia.example/early"),
            _valid(company="甲公司", recruitment_type="秋招提前批", apply_url="https://jia.example/jobs"),
            _valid(company="丙公司", industry="金融"),
            _valid(company="丁公司", recruitment_target="2026届"),
            _valid(company="戊公司", recruitment_type="秋招,实习"),
        ],
    }
    before = copy.deepcopy(snapshot)

    result = filter_oc_snapshot(snapshot)

    assert snapshot == before
    assert [lead.canonical_name for lead in result.leads] == ["乙科技", "戊公司", "甲公司"]
    assert result.rows_seen == 6
    assert result.accepted_rows == 4
    assert result.pages_fetched == 3
    company = next(lead for lead in result.leads if lead.canonical_name == "甲公司")
    assert company.metadata["source_rows"] == 2
    assert company.source_urls == (
        "https://jia.example/early",
        "https://jia.example/jobs",
    )
    assert result.metadata["filters"] == {"company_types": ["民企"]}


def test_private_type_conflict_quarantines_the_company() -> None:
    snapshot = {
        "records": [
            _valid(company="冲突公司", apply_url="https://conflict.example/private"),
            _valid(company="冲突 公司", company_type="央国企", apply_url="https://conflict.example/state"),
        ]
    }

    result = filter_oc_snapshot(snapshot)

    assert result.leads == ()
    assert result.accepted_rows == 0
    assert result.metadata["quarantined_company_type_conflicts"] == 1


def test_filter_can_read_an_agent_owned_json_file(tmp_path) -> None:
    path = tmp_path / "oc_snapshot.json"
    path.write_text(json.dumps({"records": [_valid()]}, ensure_ascii=False), encoding="utf-8")

    result = filter_oc_snapshot(path)

    assert [lead.canonical_name for lead in result.leads] == ["甲公司"]


def test_filter_excludes_known_non_job_entries_from_candidate_leads() -> None:
    snapshot = {
        "records": [
            _valid(
                company="问卷公司",
                apply_url=None,
                resolved_apply_urls=["https://wj.qq.com/s2/example/"],
                link_resolution="resolved",
            ),
            _valid(
                company="岗位公司",
                apply_url=None,
                resolved_apply_urls=["https://jobs.example.com/campus/jobs"],
                link_resolution="resolved",
            ),
        ]
    }

    result = filter_oc_snapshot(snapshot)

    assert [lead.canonical_name for lead in result.leads] == ["岗位公司"]
    assert result.accepted_rows == 1
    assert result.metadata["eligible_rows_before_entry_validation"] == 2
    assert result.metadata["excluded_non_job_entry_rows"] == 1


def test_filter_reports_versioned_exclusions_and_keeps_eligible_company_without_url() -> None:
    snapshot = {
        "records": [
            _valid(company="无地址科技", apply_url=None),
            _valid(company="国企", company_type="央国企"),
            _valid(company="旧届", recruitment_target="2026届"),
            _valid(company="春招", recruitment_type="春招"),
            _valid(company="金融", industry="金融"),
            _valid(company="", apply_url=None),
        ]
    }

    result = filter_oc_snapshot(snapshot)

    assert [lead.canonical_name for lead in result.leads] == ["无地址科技"]
    assert result.leads[0].source_urls == ()
    assert result.metadata["filter_version"] == OC_FILTER_VERSION
    assert result.metadata["exclusion_counts"] == {
        "cohort": 1,
        "company_type": 1,
        "industry": 1,
        "missing_company": 1,
        "recruitment_type": 1,
    }
    assert sum(result.metadata["exclusion_counts"].values()) + result.accepted_rows == result.rows_seen
    assert result.metadata["pagination_evidence"]["termination_reason"] == "pagination_evidence_missing"


def test_filter_records_verified_complete_pagination_evidence() -> None:
    snapshot = {
        "records": [_valid(company="甲"), _valid(company="乙")],
        "pagination": {
            "complete": True,
            "total_pages": 2,
            "total_items": 2,
            "page_counts": [1, 1],
        },
    }

    result = filter_oc_snapshot(snapshot)

    assert result.pages_fetched == 2
    assert result.metadata["pagination_evidence"] == {
        "complete": True,
        "termination_reason": "advertised_last_page_reached",
        "total_pages": 2,
        "advertised_total_items": 2,
        "page_counts_total": 2,
        "record_count": 2,
    }


def test_filter_rejects_inconsistent_pagination_evidence_as_complete() -> None:
    snapshot = {
        "records": [_valid(company="甲"), _valid(company="乙")],
        "pagination": {
            "complete": True,
            "total_pages": 2,
            "total_items": 3,
            "page_counts": [2, 1],
        },
    }

    result = filter_oc_snapshot(snapshot)

    evidence = result.metadata["pagination_evidence"]
    assert evidence["complete"] is False
    assert evidence["termination_reason"] == "pagination_evidence_inconsistent"
