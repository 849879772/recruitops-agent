from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_offerbiu_link_quality import (
    GROUPS,
    classify_apply_url,
    evaluate_snapshot,
    normalize_url,
    write_outputs,
)


def _item(
    item_id: str,
    company_id: str,
    company_name: str,
    url: object,
    groups: list[str],
) -> dict:
    return {
        "id": item_id,
        "companyId": company_id,
        "companyName": company_name,
        "industryGroupCodes": groups,
        "recruitType": "秋招",
        "targetYears": [2027],
        "applyUrl": url,
        "announcementUrl": f"https://mp.weixin.qq.com/s/{item_id}",
    }


def _snapshot(items: list[dict]) -> dict:
    return {
        "source": "offerbiu",
        "source_url": "https://offerbiu.com/api/recruitment/postings",
        "complete": True,
        "filters": {"seasonYear": 2027, "recruitType": "秋招"},
        "pages": [{"page": 0, "totalPages": 1}],
        "items": items,
    }


def test_classifies_known_non_job_entries_without_harming_ats() -> None:
    assert classify_apply_url("https://mp.weixin.qq.com/s/article") == "wechat_article"
    assert classify_apply_url("https://doc.weixin.qq.com/forms/abc") == "form"
    assert classify_apply_url("https://wj.qq.com/s2/abc") == "form"
    assert classify_apply_url("https://wj.toutiao.com/s/abc") == "form"
    assert classify_apply_url("https://v.wjx.cn/vm/abc.aspx") == "form"
    assert classify_apply_url("https://jinshuju.net/f/abc") == "form"
    assert classify_apply_url("https://www.wenjuan.com/s/abc") == "form"
    assert classify_apply_url("https://office.chaoxing.com/apps/forms/abc") == "form"
    assert classify_apply_url("https://doc.weixin.qq.com/smartsheet/form/abc") == "form"
    assert classify_apply_url("https://yunbiz.wps.cn/m/abc") == "form"
    assert classify_apply_url("https://mp.weixinbridge.com/mp/wapredirect?url=x") == "wechat_article"
    assert classify_apply_url("https://foo.zhiye.com/campus/jobs") == "official_or_unknown"
    assert classify_apply_url("https://app.mokahr.com/campus-recruitment/acme/123#/jobs") == "official_or_unknown"
    assert classify_apply_url("https://acme.jobs.feishu.cn/campus") == "official_or_unknown"
    assert classify_apply_url("https://example.com/careers") == "official_or_unknown"


def test_normalization_removes_only_fragment_and_explicit_utm() -> None:
    raw = "https://app.mokahr.com/campus/acme?project=42&utm_source=wechat&sessionid=&utm_campaign=x#/jobs?jc=2"
    assert normalize_url(raw) == "https://app.mokahr.com/campus/acme?project=42&sessionid=#/jobs?jc=2"
    assert normalize_url("https://example.com/careers#top") == "https://example.com/careers"


def test_cross_industry_companies_are_assigned_once_to_the_underfilled_bucket() -> None:
    items = [
        _item("shared", "co-shared", "交叉公司", "https://mp.weixin.qq.com/s/shared", [GROUPS[0], GROUPS[1]]),
        _item("internet", "co-internet", "互联网公司", "https://internet.zhiye.com/campus/jobs", [GROUPS[0]]),
        _item("internet-2", "co-internet-2", "互联网公司二", "https://internet-2.example.com/campus", [GROUPS[0]]),
        _item("manufacturing", "co-manufacturing", "制造公司", "https://maker.example.com/campus", [GROUPS[1]]),
    ]
    samples, _ = evaluate_snapshot(_snapshot(items))

    assigned = {
        company["company_id"]: company["assigned_industry_group"]
        for company in samples["selected_companies"]
    }
    assert assigned["co-shared"] == GROUPS[1]
    assert set(samples["groups"][GROUPS[0]]["company_keys"]).isdisjoint(
        samples["groups"][GROUPS[1]]["company_keys"]
    )
    assert len(assigned) == 4


def test_reports_underfilled_buckets_and_preserves_all_projects_for_a_company() -> None:
    items = [
        _item(
            "project-form",
            "co-multi",
            "多项目公司",
            "https://wjx.cn/vm/project",
            [GROUPS[0]],
        ),
        _item(
            "project-ats",
            "co-multi",
            "多项目公司",
            "https://app.mokahr.com/campus-recruitment/acme/42?project=7&utm_source=x#/jobs",
            [GROUPS[0]],
        ),
        _item("only-id", "co-one", "单项目公司", "not-a-url", [GROUPS[0]]),
    ]
    samples, quality = evaluate_snapshot(_snapshot(items))

    assert samples["underfilled_bucket_count"] == 3
    assert samples["groups"][GROUPS[0]]["selected_company_count"] == 2
    multi = next(company for company in samples["selected_companies"] if company["company_id"] == "co-multi")
    assert multi["record_ids"] == ["project-ats", "project-form"]
    assert {record["id"] for record in multi["records"]} == {"project-form", "project-ats"}
    assert quality["counts"] == {
        "records": 3,
        "independent_companies": 2,
        "normalized_entries": 2,
        "unique_normalized_entries": 2,
    }
    assert quality["classifications"]["records"] == {
        "official_or_unknown": 1,
        "wechat_article": 0,
        "form": 1,
        "missing": 1,
    }


def test_write_outputs_has_stable_files_and_raw_ids(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    output_dir = tmp_path / "out"
    snapshot_path.write_text(
        json.dumps(_snapshot([_item("id-1", "co-1", "公司", "https://example.com/?project=1#top", [GROUPS[0]])]), ensure_ascii=False),
        encoding="utf-8",
    )

    summary = write_outputs(snapshot_path, output_dir)
    samples = json.loads((output_dir / "samples.json").read_text(encoding="utf-8"))
    quality = json.loads((output_dir / "quality_static.json").read_text(encoding="utf-8"))

    assert summary["underfilled_bucket_count"] == 3
    assert (output_dir / "samples.json").exists()
    assert (output_dir / "quality_static.json").exists()
    assert quality["records"][0]["id"] == "id-1"
    assert quality["records"][0]["source"] == "offerbiu"
    assert quality["records"][0]["apply_url"] == "https://example.com/?project=1#top"
    assert samples["selected_records"][0]["normalized_apply_url"] == "https://example.com/?project=1"
