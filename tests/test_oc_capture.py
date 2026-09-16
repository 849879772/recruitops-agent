from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from packages.discovery import (
    MAX_OC_RECORDS,
    OcCaptureRequest,
    OcLinkResolutionSummary,
    classify_oc_destination_url,
    normalize_oc_destination_url,
    persist_oc_capture,
)


def test_oc_capture_is_validated_and_atomically_persisted(tmp_path) -> None:
    destination = tmp_path / "discovery" / "givemeoc_latest.json"
    request = OcCaptureRequest(
        captured_at=datetime(2026, 8, 26, 8, 30, tzinfo=timezone.utc),
        total_pages=2,
        total_items=2,
        page_counts=[1, 1],
        records=[
            {
                "company": "示例科技",
                "company_type": "民企",
                "industry": "人工智能/机器人",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
                "apply_urls": ["https://jobs.example.com/campus#jobs"],
            },
            {
                "company": "示例软件",
                "company_type": "民企",
                "industry": "软件",
                "recruitment_type": "秋招提前批",
                "recruitment_target": "2027届",
                "apply_urls": ["https://software.example.com/campus"],
            },
        ],
    )

    result = persist_oc_capture(request, destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert result.record_count == 2
    assert result.total_pages == 2
    assert result.total_items == 2
    assert len(result.sha256) == 64
    assert payload["filters"] == {
        "company_types": ["民企"],
        "target_candidates": "2027",
        "recruitment_types": ["秋招", "秋招提前批"],
    }
    assert payload["records"][0]["company"] == "示例科技"
    assert payload["records"][0]["apply_urls"] == ["https://jobs.example.com/campus#jobs"]
    assert payload["pagination"] == {
        "complete": True,
        "page_counts": [1, 1],
        "total_items": 2,
        "total_pages": 2,
    }
    assert not list(destination.parent.glob("*.tmp"))


def test_oc_capture_rejects_incomplete_pagination_evidence() -> None:
    with pytest.raises(ValueError, match="every advertised OC item"):
        OcCaptureRequest(
            captured_at=datetime(2026, 8, 31, 8, 30, tzinfo=timezone.utc),
            total_pages=2,
            total_items=2,
            page_counts=[1, 1],
            records=[
                {
                    "company": "仅第一页",
                    "company_type": "民企",
                    "industry": "软件",
                    "recruitment_type": "秋招",
                    "recruitment_target": "2027届",
                }
            ],
        )


def test_oc_capture_persists_resolved_apply_urls_and_link_resolution(tmp_path) -> None:
    destination = tmp_path / "discovery" / "givemeoc_latest.json"
    request = OcCaptureRequest(
        captured_at=datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc),
        total_pages=1,
        total_items=1,
        page_counts=[1],
        link_resolution=OcLinkResolutionSummary(requested=1, resolved=1, unresolved=0),
        records=[
            {
                "company": "外部招聘公司",
                "company_type": "民企",
                "industry": "人工智能",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
                "apply_urls": [
                    "https://www.givemeoc.com/wp-admin/admin-post.php?action=crt_open_link"
                ],
                "resolved_apply_urls": ["https://jobs.example.com/campus"],
                "link_resolution": "resolved",
            }
        ],
    )

    result = persist_oc_capture(request, destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert result.link_resolution.model_dump() == {
        "requested": 1,
        "resolved": 1,
        "unresolved": 0,
        "login_fallbacks": 0,
        "login_required": False,
        "addressable": 0,
        "excluded": 0,
    }
    assert payload["link_resolution"] == {
        "requested": 1,
        "resolved": 1,
        "unresolved": 0,
        "login_fallbacks": 0,
        "login_required": False,
        "addressable": 0,
        "excluded": 0,
    }
    assert payload["records"][0]["resolved_apply_urls"] == [
        "https://jobs.example.com/campus"
    ]
    assert payload["records"][0]["link_resolution"] == "resolved"


def test_oc_capture_accepts_current_scale_and_mixed_login_fallbacks() -> None:
    total = 1_559
    assert MAX_OC_RECORDS >= total
    request = OcCaptureRequest(
        captured_at=datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc),
        total_pages=52,
        total_items=total,
        page_counts=[30] * 51 + [29],
        link_resolution=OcLinkResolutionSummary(
            requested=705,
            resolved=650,
            unresolved=55,
            login_fallbacks=12,
            login_required=False,
        ),
        records=[
            {
                "company": f"测试公司-{index}",
                "company_type": "民企",
                "industry": "科技",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
            }
            for index in range(total)
        ],
    )

    assert request.total_items == total
    assert request.link_resolution.login_fallbacks == 12


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://wj.qq.com/s2/12345/", "form"),
        ("https://docs.qq.com/form/page/abc", "form"),
        ("https://jizhicar.wjx.cn/vm/example.aspx", "form"),
        ("https://alidocs.dingtalk.com/notable/share/form/example", "form"),
        ("https://mp.weixin.qq.com/s/example", "article"),
        ("https://mp.weixinbridge.com/mp/wapredirect?url=x", "article"),
        ("https://distribute.ebiaoge.com/sp/formreport/abc", "form"),
        ("https://www.zhipin.com/job_detail/example.html", "third_party_listing"),
        ("https://jobs.example.com/login.html", "login_page"),
        ("https://app.mokahr.com/campus/a/1#/candidateHome/applications", "application_record"),
    ],
)
def test_oc_destination_classifies_non_job_entries(url: str, kind: str) -> None:
    classification = classify_oc_destination_url(url)

    assert classification is not None
    assert classification[0] == kind
    assert classify_oc_destination_url("https://jobs.example.com/campus/jobs") is None


def test_oc_destination_url_normalizes_html_entities_and_default_ports() -> None:
    assert normalize_oc_destination_url(
        " HTTPS://Jobs.Example.com:443/campus?x=1&amp;y=2 "
    ) == "https://jobs.example.com/campus?x=1&y=2"


def test_persistence_moves_non_job_destinations_out_of_addressable_urls(tmp_path) -> None:
    destination = tmp_path / "discovery" / "givemeoc_latest.json"
    request = OcCaptureRequest(
        captured_at=datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc),
        total_pages=1,
        total_items=1,
        page_counts=[1],
        link_resolution=OcLinkResolutionSummary(
            requested=1,
            resolved=1,
            unresolved=0,
            excluded=1,
        ),
        records=[
            {
                "company": "问卷公司",
                "company_type": "民企",
                "industry": "软件",
                "recruitment_type": "秋招",
                "recruitment_target": "2027届",
                "resolved_apply_urls": ["https://wj.qq.com/s2/12345/"],
                "link_resolution": "resolved",
            }
        ],
    )

    persist_oc_capture(request, destination)
    record = json.loads(destination.read_text(encoding="utf-8"))["records"][0]

    assert record["resolved_apply_urls"] == []
    assert record["link_resolution"] == "excluded_non_job_entry"
    assert record["excluded_apply_urls"] == [
        {
            "url": "https://wj.qq.com/s2/12345/",
            "kind": "form",
            "reason": "Tencent questionnaire/form is not a reusable recruitment job list.",
        }
    ]
