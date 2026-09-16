from __future__ import annotations

import json

import yaml

from scripts.report_oc_pending_candidates import build_report


def test_report_includes_pending_existing_and_new_leads(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "captured_at": "2026-09-01T08:00:00+00:00",
        "pagination": {"total_pages": 1},
        "records": [
            {
                "company": "Pending Co",
                "company_type": "民企",
                "industry": "科技",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
                "resolved_apply_urls": ["https://pending.jobs.feishu.cn/1/position/list"],
            },
            {
                "company": "New Co",
                "company_type": "民企",
                "industry": "科技",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
                "resolved_apply_urls": [],
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")
    companies = tmp_path / "companies.yaml"
    companies.write_text(yaml.safe_dump({"companies": [{
        "name": "Pending Co",
        "source_identity": "feishu:pending:1",
        "integration_status": "not_connected",
    }]}, allow_unicode=True), encoding="utf-8")

    report = build_report(snapshot, companies)

    assert report["total"] == 2
    assert report["addressable"] == 1
    assert report["entry_discovery_required"] == 1
    assert report["entry_kind_counts"] == {
        "existing_adapter": 1,
        "no_public_url": 1,
    }
