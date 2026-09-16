from __future__ import annotations

from argparse import Namespace
import json

from packages.recruitment_core.jd_repair import (
    content_sha256,
    repair_decision,
    truncation_evidence,
    validate_candidate,
    validate_provenance,
)
from packages.recruitment_core import job_details
from packages.recruitment_core.crawlers.feishu import GenericFeishuCrawler


def _job(raw: str, **changes):
    value = {
        "id": "job-1",
        "company_id": "company-1",
        "company_name": "小鹏汽车",
        "title": "【27届校招】具身智能AI软件工程师",
        "city": "广州",
        "detail_url": "https://xiaopeng.jobs.feishu.cn/398875/position/123/detail",
        "jd_raw": raw,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "source_platform": "feishu",
        "source_tenant": "feishu:xiaopeng.jobs.feishu.cn",
        "company_campus_url": "https://xiaopeng.jobs.feishu.cn/398875/position",
        "native_job_id": "native-123",
    }
    value.update(changes)
    value.setdefault(
        "capture_evidence",
        {
            "status": "complete",
            "method": "test_fixture",
            "source_url": value["detail_url"],
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": content_sha256(raw),
        },
    )
    return value


def _raw_500(*, terminal: bool = False, both_sections: bool = False) -> str:
    prefix = "岗位职责\n负责机器人数据采集、训练和部署，构建可持续迭代的数据闭"
    if both_sections:
        prefix += "\n任职要求\n熟悉 Python、C++、Linux，具备良好工程能力。"
    filler = " 参与跨团队协作并持续优化系统性能"
    raw = (prefix + filler * 30)[:500]
    if terminal:
        raw = raw[:-1] + "。"
    return raw


def test_exact_500_is_selected_only_with_cut_evidence() -> None:
    truncated = _job(_raw_500())
    assert len(truncated["jd_raw"]) == 500
    assert truncation_evidence(truncated)
    assert repair_decision(truncated)["selected"]

    complete = _job(_raw_500(terminal=True, both_sections=True))
    assert len(complete["jd_raw"]) == 500
    assert truncation_evidence(complete) == ()
    assert repair_decision(complete)["selected"] is False
    assert repair_decision(complete)["reason"] == "stored_jd_not_proven_incomplete"


def test_provenance_rejects_other_company_and_list_route() -> None:
    job = _job(_raw_500())
    assert validate_provenance(job).ok
    other = _job(_raw_500(), detail_url="https://other.jobs.feishu.cn/398875/position/123/detail")
    assert validate_provenance(other).failure_reason == "company_host_mismatch"
    listed = _job(_raw_500(), detail_url="https://xiaopeng.jobs.feishu.cn/398875/position/list")
    assert validate_provenance(listed).status == "not_detail_route"


def test_feishu_list_and_detail_project_paths_bind_without_position_on_list() -> None:
    same_project = _job(
        _raw_500(),
        company_campus_url="https://xiaopeng.jobs.feishu.cn/398875/",
    )
    assert validate_provenance(same_project).ok

    different_project = _job(
        _raw_500(),
        company_campus_url="https://xiaopeng.jobs.feishu.cn/398876/",
    )
    assert validate_provenance(different_project).failure_reason == "feishu_campaign_scope_mismatch"


def test_feishu_root_without_project_is_not_guessed() -> None:
    root = _job(_raw_500(), company_campus_url="https://xiaopeng.jobs.feishu.cn/")
    check = validate_provenance(root)
    assert check.failure_reason == "feishu_campaign_scope_unverified"

    trusted = {
        **root,
        "recruitment_campaign_id": "398875",
    }
    assert validate_provenance(trusted).ok


def test_feishu_application_route_keeps_its_campaign_without_relaxing_scope() -> None:
    base = "https://xiaopeng.jobs.feishu.cn"
    same = _job(_raw_500(), company_campus_url=base + "/398875/position/application")
    assert validate_provenance(same).ok
    different = {**same, "company_campus_url": base + "/398876/position/application"}
    assert validate_provenance(different).failure_reason == "feishu_campaign_scope_mismatch"
    root = {**same, "company_campus_url": base + "/position/application"}
    assert validate_provenance(root).failure_reason == "feishu_campaign_scope_unverified"


def test_feishu_mobile_entry_is_canonicalized_to_the_same_project() -> None:
    base = "https://qcnhg4ksaiwt.jobs.feishu.cn"
    job = _job(
        _raw_500(),
        detail_url=base + "/257870/position/7669699060089997577/detail",
        company_campus_url=base + "/257870/m/?external_referral_code=92AJ8US",
        source_tenant="feishu:qcnhg4ksaiwt.jobs.feishu.cn",
    )

    check = validate_provenance(job)

    assert check.ok
    assert check.status == "company_and_campaign_bound"
    assert "company_campus_url:257870" in check.evidence
    assert "detail_project:257870" in check.evidence


def test_feishu_mobile_entry_with_spread_query_is_canonicalized() -> None:
    base = "https://iucylxooqp.jobs.feishu.cn"
    job = _job(
        _raw_500(),
        detail_url=base + "/200839/position/7661947654499027209/detail",
        company_campus_url=base + "/200839/m/?spread=FD7PRUV",
        source_tenant="feishu:iucylxooqp.jobs.feishu.cn",
    )

    assert validate_provenance(job).ok


def test_feishu_mobile_entry_does_not_cross_project_or_tenant() -> None:
    base = "https://qcnhg4ksaiwt.jobs.feishu.cn"
    same = _job(
        _raw_500(),
        detail_url=base + "/257870/position/7669699060089997577/detail",
        company_campus_url=base + "/257870/m/?spread=FD7PRUV",
        source_tenant="feishu:qcnhg4ksaiwt.jobs.feishu.cn",
    )
    different_project = {
        **same,
        "company_campus_url": base + "/200839/m/?spread=FD7PRUV",
    }
    different_tenant = {
        **same,
        "company_campus_url": "https://other.jobs.feishu.cn/257870/m/?spread=FD7PRUV",
    }

    assert validate_provenance(different_project).failure_reason == "feishu_campaign_scope_mismatch"
    assert validate_provenance(different_tenant).failure_reason == "company_host_mismatch"


def test_feishu_shared_root_mobile_entry_stays_unverified() -> None:
    job = _job(
        _raw_500(),
        detail_url="https://jobs.feishu.cn/257870/position/123/detail",
        company_campus_url="https://jobs.feishu.cn/m/?spread=FD7PRUV",
        source_tenant="feishu:jobs.feishu.cn",
    )

    assert validate_provenance(job).failure_reason == "feishu_campaign_scope_unverified"


def test_feishu_mobile_fix_does_not_accept_referral_or_short_alias_paths() -> None:
    base = "https://qcnhg4ksaiwt.jobs.feishu.cn"
    job = _job(
        _raw_500(),
        detail_url=base + "/257870/position/7669699060089997577/detail",
        source_tenant="feishu:qcnhg4ksaiwt.jobs.feishu.cn",
    )

    for alias in ("/257870/s/?external_referral_code=92AJ8US", "/257870/referral?spread=FD7PRUV"):
        check = validate_provenance({**job, "company_campus_url": base + alias})
        assert check.failure_reason == "feishu_campaign_scope_mismatch"


def test_candidate_requires_identity_and_complete_content() -> None:
    job = _job(_raw_500())
    full = {
        "detail": "岗位职责\n负责机器人系统开发和部署。\n任职要求\n熟悉 Python、C++、Linux。" * 16,
        "status": "complete",
        "source": "feishu_api",
        "detail_url": job["detail_url"],
        "identity_status": "matched",
        "identity_evidence": ["native_id:123", "title:【27届校招】具身智能AI软件工程师"],
    }
    full["capture_evidence"] = {
        "status": "complete",
        "method": "feishu_api",
        "source_url": full["detail_url"],
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": content_sha256(full["detail"]),
    }
    checked = validate_candidate(job, full)
    assert checked["passed"] is True
    assert checked["candidate_chars"] > 500
    assert checked["candidate_sha256"] == content_sha256(full["detail"])
    assert checked["capture_evidence"] == full["capture_evidence"]

    unchecked = {**full, "identity_status": "identity_mismatch", "identity_evidence": []}
    failed = validate_candidate(job, unchecked)
    assert failed["passed"] is False
    assert "identity_unverified" in failed["failure_reasons"]


def test_candidate_accepts_only_terminal_identity_bound_inline_list_capture() -> None:
    detail = "岗位职责\n负责机器人系统研发。\n任职要求\n熟悉 Python、C++ 和 Linux。"
    job = _job(
        "",
        detail_url="https://example.test/campus/jobs",
        company_campus_url="https://example.test/campus/jobs",
        link_kind="list",
        source_platform="custom_render",
        source_tenant="",
    )
    hydration = {
        "detail": detail,
        "status": "complete",
        "source": "configured_page_render",
        "detail_url": job["detail_url"],
        "identity_status": "matched",
        "identity_evidence": ["title:机器人软件工程师"],
        "capture_evidence": {
            "status": "complete",
            "method": "detail_interaction:inline",
            "source_url": job["detail_url"],
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": content_sha256(detail),
        },
    }

    assert validate_candidate(job, hydration)["passed"] is True

    unverified = {
        **hydration,
        "capture_evidence": {**hydration["capture_evidence"], "terminal_observed": False},
    }
    checked = validate_candidate(job, unverified)
    assert checked["passed"] is False
    assert "list_url_not_allowed" in checked["failure_reasons"]
    assert "list_page_source_not_allowed" in checked["failure_reasons"]


def test_feishu_api_distinguishes_route_id_from_internal_job_id(monkeypatch) -> None:
    body = {
        "code": 0,
        "data": {
            "job_post_detail": {
                "id": "123",
                "job_id": "internal-456",
                "title": "机器人软件工程师",
                "description": "负责机器人系统开发和部署。" * 40,
                "requirement": "熟悉 Python、C++、Linux。" * 20,
            }
        },
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response())
    result = job_details.fetch_full_job_description_result({
        "title": "机器人软件工程师",
        "company": "Example",
        "jd_raw": "",
        "jd_url": "https://example.jobs.feishu.cn/398875/position/123/detail",
        "native_job_id": "",
        "source_job_id": "",
    })
    assert result.status == "complete"
    assert result.identity_status == "request_bound"
    assert "native_id:123" in result.identity_evidence
    assert "internal_job_id:internal-456" in result.identity_evidence


def test_conflicting_identity_aliases_remain_rejected() -> None:
    status, _ = job_details._check_identity(
        {"title": "Robot Developer"},
        {"title": "Robot Developer", "name": "Sales Manager"},
        title_fields=("title", "name"),
    )
    assert status == "identity_mismatch"


def test_feishu_wrong_post_id_cannot_match_via_internal_id(monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"job_post_detail": {
                "id": "999", "job_id": "123", "title": "Robot Developer",
                "description": "Develop robot control software.",
                "requirement": "Proficient in C++ and Python.",
            }}}

    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response())
    result = job_details.fetch_feishu_job_description_status(
        "https://example.jobs.feishu.cn/398875/position/123/detail",
        identity={"title": "Robot Developer"},
    )
    assert result[1] == "identity_mismatch"


def test_feishu_api_business_error_is_not_identity_conflict(monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 1001, "message": "暂不可用", "data": {}}

    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response())
    result = job_details.fetch_feishu_job_description_status(
        "https://example.jobs.feishu.cn/398875/position/123/detail",
        identity={"title": "机器人软件工程师"},
    )
    assert result[1] == "fetch_failed"
    assert getattr(result, "identity_status", "") == ""
    assert getattr(result, "identity_evidence", ()) == ()


def test_feishu_api_detail_is_not_capped_and_dom_card_is_not_jd() -> None:
    crawler = GenericFeishuCrawler("Example", "https://tenant.jobs.feishu.cn/campus/")
    body = "负责机器人系统开发和部署。" * 80
    parsed = crawler._parse_api_payload(
        {
            "code": 0,
            "data": {"count": 1, "job_post_list": [{
                "id": 101, "title": "机器人软件工程师", "description": body,
                "requirement": "熟悉 Python、C++。",
            }]},
        },
        "https://tenant.jobs.feishu.cn/campus/position/list",
        "https://tenant.jobs.feishu.cn/api/v1/search/job/posts?offset=0&limit=10",
    )
    assert len(parsed["jobs"][0]["jd_raw"]) > 500

    from bs4 import BeautifulSoup
    anchors = BeautifulSoup(
        '<a href="/campus/position/101/detail"><span class="positionItem-title-text">机器人软件工程师</span>'
        f"<div>{body}</div></a>",
        "html.parser",
    ).find_all("a")
    dom_job = crawler._parse_anchors(anchors)[0]
    assert dom_job["jd_raw"] == ""
