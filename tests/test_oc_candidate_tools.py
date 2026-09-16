from __future__ import annotations

import json
from hashlib import sha256

import pytest
import yaml

import packages.recruitment_core.crawlers.declarative as declarative_module
import packages.tools.oc_candidates as oc_candidates_module
from packages.recruitment_core import job_cohorts
from packages.tools.oc_candidates import (
    OcAdapterCandidateTestInput,
    OcCandidateCrawlBatchInput,
    OcCandidateListInput,
    OcCandidatePageObserveInput,
    OcCandidateRunner,
    infer_candidate_crawler,
)
from packages.discovery import classify_oc_destination_url


def _capture_evidence(detail: str, source_url: str) -> dict[str, object]:
    return {
        "status": "complete",
        "method": "fixture_detail",
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.strip().encode("utf-8")).hexdigest(),
    }


def _write_fixture(tmp_path):
    snapshot = tmp_path / "givemeoc_latest.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "2026-09-01T08:00:00+00:00",
                "pagination": {"total_pages": 1},
                "records": [
                    {
                        "company": "已有科技",
                        "company_type": "民企",
                        "industry": "科技",
                        "recruitment_type": "秋招",
                        "recruitment_target": "2027届",
                        "apply_urls": ["https://www.givemeoc.com/wp-admin/admin-post.php?action=crt_open_link"],
                        "resolved_apply_urls": ["https://existing.zhiye.com/campus/jobs"],
                    },
                    {
                        "company": "新公司",
                        "company_type": "民企",
                        "industry": "人工智能",
                        "recruitment_type": "秋招提前批/实习",
                        "recruitment_target": "2027届",
                        "apply_urls": ["https://www.givemeoc.com/wp-admin/admin-post.php?action=crt_open_link"],
                        "resolved_apply_urls": ["https://app.mokahr.com/campus-recruitment/new/1"],
                    },
                    {
                        "company": "无地址公司",
                        "company_type": "民企",
                        "industry": "软件",
                        "recruitment_type": "秋招",
                        "recruitment_target": "2027届",
                        "apply_urls": ["https://www.givemeoc.com/wp-admin/admin-post.php?action=crt_open_link"],
                        "resolved_apply_urls": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "name": "已有科技",
                        "careers_url": "https://existing.zhiye.com/campus/jobs",
                        "crawler": "beisen",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return snapshot, companies


def test_infer_candidate_crawler_rejects_oc_and_recognizes_platforms() -> None:
    assert infer_candidate_crawler("https://www.givemeoc.com/x") is None
    assert infer_candidate_crawler("https://app.mokahr.com/campus-recruitment/a/1") == "moka"
    assert infer_candidate_crawler("https://acme.jobs.feishu.cn/campus/position") == "feishu"
    assert infer_candidate_crawler("https://acme.zhiye.com/campus/jobs") == "beisen"
    assert infer_candidate_crawler("https://acme.m.zhiye.com/#/jobs") == "beisen_mobile"
    assert infer_candidate_crawler("https://wecruit.hotjob.cn/x") == "hotjob"
    assert infer_candidate_crawler("https://career.huawei.com/cn/campus-recruitment") == "huawei"
    assert infer_candidate_crawler("https://join.qq.com/post.html") == "tencent"
    assert infer_candidate_crawler("https://talent.baidu.com/jobs/") == "baidu"
    assert infer_candidate_crawler("https://campus.163.com/app/job/position?id=103") == "netease"
    assert infer_candidate_crawler("https://jobs.bytedance.com/campus/position") == "bytedance"
    assert infer_candidate_crawler(
        "https://app135149.dingtalkoxm.com/campus-recruitment/acme/100"
    ) == "moka"
    assert infer_candidate_crawler(
        "https://jobs.example.com/campus/position/list?spread=ABC"
    ) == "feishu"
    assert infer_candidate_crawler(
        "https://jobs.bilibili.com/campus/positions?type=3"
    ) == "bilibili"
    assert infer_candidate_crawler("https://jobs.example.com/campus") == "render"
    assert infer_candidate_crawler("http://127.0.0.1/jobs") is None


def test_public_candidate_crawl_does_not_read_retired_oc_snapshot(tmp_path) -> None:
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n", encoding="utf-8")
    runner = OcCandidateRunner(
        tmp_path / "retired-source-does-not-exist.json",
        companies,
        process=lambda **_kwargs: [],
    )

    result = runner.crawl_public_candidate(
        "测试公司",
        "https://jobs.example.com/campus",
        OcCandidateCrawlBatchInput(
            company_names=["测试公司"],
            require_complete_jd=False,
        ),
    )

    assert result.company == "测试公司"
    assert result.source_url == "https://jobs.example.com/campus"


def test_oc_destination_filter_rejects_wechat_bridge_subdomains_and_wps_forms() -> None:
    assert classify_oc_destination_url("https://open.mp.weixinbridge.com/article/1")[0] == "article"
    assert classify_oc_destination_url("https://yunbiz.wps.cn/form/abc")[0] == "form"


def test_empty_known_adapter_is_attributed_to_adapter_variant(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["records"].append({
        "company": "腾讯变体",
        "company_type": "民企",
        "industry": "科技",
        "recruitment_type": "秋招",
        "recruitment_target": "2027届",
        "resolved_apply_urls": ["https://join.qq.com/"],
    })
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = OcCandidateRunner(snapshot, companies, process=lambda **_kwargs: []).crawl(
        OcCandidateCrawlBatchInput(company_names=["腾讯变体"])
    )

    assert response.data is not None
    result = response.data.results[0]
    assert result.crawler_key == "tencent"
    assert result.integration_status == "needs_adapter"
    assert result.error_code == "adapter_variant_unsupported"


def test_root_entry_discovery_uses_discovered_feishu_adapter(tmp_path, monkeypatch) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["records"].append({
        "company": "入口发现公司",
        "company_type": "民企",
        "industry": "科技",
        "recruitment_type": "秋招",
        "recruitment_target": "2027届",
        "resolved_apply_urls": ["https://www.entry-example.test/"],
    })
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        oc_candidates_module,
        "render_page",
        lambda *_args, **_kwargs: (
            '<a href="https://entry-example.jobs.feishu.cn/campus/position">校园招聘</a>'
        ),
    )
    calls = []

    def process(**kwargs):
        calls.append((kwargs["crawler_key"], kwargs["source_url"]))
        if kwargs["crawler_key"] == "feishu":
            return [{
                "id": "job-1",
                "title": "软件开发工程师",
                "jd_url": "https://entry-example.jobs.feishu.cn/campus/position/1",
                "jd_raw": "负责软件开发和测试，要求熟悉 Python。",
                "capture_evidence": _capture_evidence(
                    "负责软件开发和测试，要求熟悉 Python。",
                    "https://entry-example.jobs.feishu.cn/campus/position/1",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            }]
        return []

    response = OcCandidateRunner(snapshot, companies, process=process).crawl(
        OcCandidateCrawlBatchInput(company_names=["入口发现公司"])
    )

    assert response.data is not None
    result = response.data.results[0]
    assert calls == [
        ("render", "https://www.entry-example.test/"),
        ("feishu", "https://entry-example.jobs.feishu.cn/campus/position"),
    ]
    assert result.status == "succeeded"
    assert result.crawler_key == "feishu"
    assert result.discovered_entry_url == "https://entry-example.jobs.feishu.cn/campus/position"


def test_candidate_list_exposes_new_and_resolved_counts(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    runner = OcCandidateRunner(snapshot, companies)

    response = runner.list(OcCandidateListInput(limit=10))

    assert response.success
    assert response.data is not None
    assert response.data.total_candidates == 2
    assert response.data.resolved_candidates == 1
    assert response.data.unresolved_candidates == 1
    assert [item.company for item in response.data.candidates] == ["新公司", "无地址公司"]
    new_company = response.data.candidates[0]
    assert new_company.resolved_urls == ["https://app.mokahr.com/campus-recruitment/new/1"]
    assert new_company.inferred_crawlers == ["moka"]
    assert new_company.entry_diagnoses[0].entry_kind == "existing_adapter"
    assert new_company.approval_state == "needs_isolated_test"
    assert new_company.runtime_enabled is False


def test_candidate_list_filters_known_company_names_before_pagination(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    runner = OcCandidateRunner(snapshot, companies)

    response = runner.list(OcCandidateListInput(
        company_names=["无地址公司"],
        offset=0,
        limit=10,
    ))

    assert response.success
    assert response.data is not None
    assert response.data.total_candidates == 1
    assert response.data.resolved_candidates == 0
    assert response.data.unresolved_candidates == 1
    assert [item.company for item in response.data.candidates] == ["无地址公司"]


def test_agent_can_observe_and_test_generated_dom_candidate(tmp_path, monkeypatch) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["records"].append({
        "company": "自建招聘站",
        "company_type": "民企",
        "industry": "科技",
        "recruitment_type": "秋招",
        "recruitment_target": "2027届",
        "resolved_apply_urls": ["https://careers.example.test/campus/jobs"],
    })
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    listing = """
    <main>
      <a class="job-card" href="/campus/jobs/1"><h3>软件开发工程师</h3></a>
      <a class="job-card" href="/campus/jobs/2"><h3>算法工程师</h3></a>
    </main>
    """
    detail = (
        "<article><h1>岗位详情</h1><p>岗位职责：负责软件系统设计、开发、测试和持续优化。"
        "任职要求：熟悉 Python、数据结构、数据库和工程实践，具备良好的沟通协作能力。"
        "能够独立分析问题并完成高质量交付。</p></article>"
    )

    def fake_render(url, **_kwargs):
        return listing if url.endswith("/campus/jobs") else detail

    monkeypatch.setattr(oc_candidates_module, "render_page", fake_render)
    monkeypatch.setattr(
        oc_candidates_module,
        "observe_page_with_network",
        lambda url, **_kwargs: {"html": fake_render(url), "responses": []},
    )
    monkeypatch.setattr(declarative_module, "render_page", fake_render)

    def fake_live_candidate_process(**kwargs):
        crawler = declarative_module.DeclarativeRecruitCrawler(
            kwargs["company"],
            kwargs["source_url"],
            kwargs["recipe"],
            page_renderer=fake_render,
        )
        jobs = crawler.fetch()
        for job in jobs:
            job["recruitment_track"] = "formal"
            job["capture_evidence"] = _capture_evidence(job["jd_raw"], job["jd_url"])
        return {
            "ok": True,
            "jobs": jobs,
            "pagination_complete": crawler.pagination_complete,
            "advertised_total": crawler.expected_total,
        }

    runner = OcCandidateRunner(
        snapshot,
        companies,
        live_candidate_process=fake_live_candidate_process,
    )

    observed = runner.observe_page(OcCandidatePageObserveInput(
        company_name="自建招聘站",
        outline_limit=40,
    ))
    assert observed.success
    assert observed.data is not None
    assert any(node.text == "软件开发工程师" for node in observed.data.nodes)
    assert observed.data.selector_candidates

    tested = runner.test_dom_candidate(OcAdapterCandidateTestInput(
        company_name="自建招聘站",
        snapshot_id=observed.data.snapshot_id,
        title_selector="a.job-card h3",
        expected_min_jobs=2,
        require_complete_jd=True,
    ))
    assert tested.success
    assert tested.data is not None
    assert tested.data.state == "awaiting_approval"
    assert tested.data.raw_job_count == 2
    assert tested.data.accepted_count == 2
    assert tested.data.complete_jd_count == 2
    assert tested.data.runtime_enabled is False

    tested_recipe = runner.test_dom_candidate(OcAdapterCandidateTestInput(
        company_name="自建招聘站",
        snapshot_id=observed.data.snapshot_id,
        recipe={
            "type": "html_list",
            "listing_url": "https://careers.example.test/campus/jobs",
            "list_selector": "a.job-card",
            "title_selector": "h3",
            "detail_link_selector": "a[href]",
        },
        expected_min_jobs=2,
        require_complete_jd=True,
    ))
    assert tested_recipe.success
    assert tested_recipe.data is not None
    assert tested_recipe.data.recipe_type == "html_list"


def test_agent_can_bind_api_recipe_to_frozen_network_observation(tmp_path, monkeypatch) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["records"].append({
        "company": "接口招聘站",
        "company_type": "民企",
        "industry": "科技",
        "recruitment_type": "秋招",
        "recruitment_target": "2027届",
        "resolved_apply_urls": ["https://api-careers.example.test/jobs"],
    })
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    api_url = "https://api-careers.example.test/api/jobs"
    monkeypatch.setattr(oc_candidates_module, "observe_page_with_network", lambda *_args, **_kwargs: {
        "html": "<main><h1>校园招聘</h1></main>",
        "responses": [{
            "url": api_url,
            "method": "POST",
            "status": 200,
            "content_type": "application/json",
            "sha256": "a" * 64,
            "paths": ["$", "$.data", "$.data.rows"],
            "array_paths": {"$.data.rows": 1},
            "scalar_samples": {"$.data.rows.0.title": "算法工程师"},
            "request_body": {"page": 1, "pageSize": 20},
            "payload": {"data": {"rows": [{"id": "1", "title": "算法工程师"}], "total": 1}},
        }],
    })

    def live(**_kwargs):
        return {
            "jobs": [{
                "id": "1",
                "title": "算法工程师",
                "jd_url": "https://api-careers.example.test/jobs/1",
                "jd_raw": (
                    "岗位职责：负责算法研发、测试、部署和持续优化。"
                    "任职要求：熟悉 Python、机器学习框架，具备算法工程实践经验。"
                ),
                "capture_evidence": _capture_evidence(
                    "岗位职责：负责算法研发、测试、部署和持续优化。"
                    "任职要求：熟悉 Python、机器学习框架，具备算法工程实践经验。",
                    "https://api-careers.example.test/jobs/1",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            }],
            "pagination_complete": True,
            "advertised_total": 1,
        }

    runner = OcCandidateRunner(snapshot, companies, live_candidate_process=live)
    observed = runner.observe_page(OcCandidatePageObserveInput(company_name="接口招聘站"))
    assert observed.data is not None
    assert observed.data.json_candidates[0].array_paths == {"$.data.rows": 1}

    tested = runner.test_dom_candidate(OcAdapterCandidateTestInput(
        company_name="接口招聘站",
        snapshot_id=observed.data.snapshot_id,
        recipe={
            "type": "api_campaigns",
            "request": {"method": "POST", "url": api_url, "body": {"page": 1, "pageSize": 20}},
            "items_path": "$.data.rows",
            "total_path": "$.data.total",
            "field_map": {"id": "id", "title": "title", "jd": []},
            "detail_url_template": "https://api-careers.example.test/jobs/{id}",
        },
    ))
    assert tested.success
    assert tested.data is not None
    assert tested.data.recipe_type == "api_campaigns"


def test_candidate_list_includes_configured_not_connected_company(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = yaml.safe_load(companies.read_text(encoding="utf-8"))
    payload["companies"][0]["integration_status"] = "not_connected"
    companies.write_text(
        yaml.safe_dump(payload, allow_unicode=True),
        encoding="utf-8",
    )

    response = OcCandidateRunner(snapshot, companies).list(OcCandidateListInput(limit=10))

    assert response.success
    assert response.data is not None
    assert response.data.total_candidates == 3
    assert [item.company for item in response.data.candidates] == [
        "已有科技",
        "新公司",
        "无地址公司",
    ]


def test_candidate_list_uses_unique_source_identity_for_ambiguous_unit(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    payload = yaml.safe_load(companies.read_text(encoding="utf-8"))
    payload["companies"] = [
        {
            "name": "集团公司",
            "aliases": ["已有科技"],
            "source_identity": "web:group.example:/campus",
            "integration_status": "connected",
        },
        {
            "name": "已有科技",
            "source_identity": "beisen:existing.zhiye.com:/campus/jobs:",
            "integration_status": "not_connected",
        },
    ]
    companies.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    response = OcCandidateRunner(snapshot, companies).list(OcCandidateListInput(limit=10))

    assert response.success
    assert response.data is not None
    assert "已有科技" in [item.company for item in response.data.candidates]


def test_candidate_batch_crawls_only_resolved_new_company(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    calls = []

    def process(**kwargs):
        calls.append(kwargs)
        return [
            {
                "id": "job-1",
                "title": "软件开发工程师",
                "city": "深圳",
                "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/1",
                "jd_raw": "负责软件开发与测试，要求熟悉 Python。",
                "capture_evidence": _capture_evidence(
                    "负责软件开发与测试，要求熟悉 Python。",
                    "https://app.mokahr.com/campus-recruitment/new/1#/jobs/1",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            }
        ]

    runner = OcCandidateRunner(snapshot, companies, process=process)
    response = runner.crawl(
        OcCandidateCrawlBatchInput(company_names=["新公司", "无地址公司"], limit=2)
    )

    assert response.success
    assert response.data is not None
    assert response.data.total_candidates == 2
    assert response.data.selected_count == 2
    assert response.data.addressable_count == 1
    assert response.data.attempted_count == 1
    assert response.data.succeeded_count == 1
    assert len(calls) == 1
    assert calls[0]["crawler_key"] == "moka"
    assert calls[0]["source_context"] == {
        "source_cohort": 2027,
        "source_cohort_source": "OC固定筛选2027届秋招",
        "source_cohort_evidence": "OC固定筛选记录：招聘对象=2027届；招聘类型=秋招提前批/实习",
        "source_cohort_url": "https://app.mokahr.com/campus-recruitment/new/1",
    }
    assert response.data.results[0].status == "succeeded"
    assert response.data.results[0].candidate_kind == "reuse"
    assert response.data.results[0].approval_state == "awaiting_approval"
    assert response.data.results[0].runtime_enabled is False
    assert response.data.results[1].status == "not_addressable"


def test_candidate_batch_normalizes_legacy_zero_cohort_as_unconfirmed(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**_kwargs):
        return [
            {
                "id": "legacy-job",
                "title": "软件开发工程师",
                "city": "深圳",
                "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/legacy",
                "jd_raw": "负责软件开发与测试。",
                "capture_evidence": _capture_evidence(
                    "负责软件开发与测试。",
                    "https://app.mokahr.com/campus-recruitment/new/1#/jobs/legacy",
                ),
                "cohort": 0,
                "cohort_status": "unconfirmed",
                "recruitment_track": "formal",
            }
        ]

    runner = OcCandidateRunner(snapshot, companies, process=process)
    response = runner.crawl(OcCandidateCrawlBatchInput(company_names=["新公司"]))

    assert response.success
    assert response.data is not None
    result = response.data.results[0]
    assert result.raw_job_count == 1
    assert result.error_code != "crawler_failed"


def test_candidate_batch_reports_composite_cohort_evidence(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**_kwargs):
        return [
            {
                "id": "composite-job",
                "title": "机器人软件工程师",
                "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/composite",
                "jd_raw": "负责机器人软件开发与测试。",
                "capture_evidence": _capture_evidence(
                    "负责机器人软件开发与测试。",
                    "https://app.mokahr.com/campus-recruitment/new/1#/jobs/composite",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "cohort_source": "OC 2027届秋招 + 公司官方校招门户",
                "cohort_evidence": "OC 2027届秋招；官网证据：新公司校园招聘",
                "recruitment_track": "formal",
            }
        ]

    response = OcCandidateRunner(snapshot, companies, process=process).crawl(
        OcCandidateCrawlBatchInput(company_names=["新公司"])
    )

    assert response.data is not None
    result = response.data.results[0]
    assert result.status == "succeeded"
    assert result.composite_cohort_count == 1
    assert result.cohort_evidence == ["OC 2027届秋招；官网证据：新公司校园招聘"]


def test_candidate_batch_reports_partial_instead_of_false_success(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**_kwargs):
        return {
            "jobs": [
                {
                    "id": "partial-job",
                    "title": "软件开发工程师",
                    "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/partial",
                    "jd_raw": "负责软件开发与测试。",
                    "capture_evidence": _capture_evidence(
                        "负责软件开发与测试。",
                        "https://app.mokahr.com/campus-recruitment/new/1#/jobs/partial",
                    ),
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "recruitment_track": "formal",
                }
            ],
            "pagination_complete": False,
            "completeness_known": True,
            "pages_seen": 1,
            "total_pages": 3,
            "has_more": True,
            "advertised_total": 9,
            "termination_reasons": ["next_navigation_failed"],
        }

    response = OcCandidateRunner(snapshot, companies, process=process).crawl(
        OcCandidateCrawlBatchInput(company_names=["新公司"])
    )

    assert response.data is not None
    result = response.data.results[0]
    assert result.status == "failed"
    assert result.integration_status == "connected_partial"
    assert result.pagination_complete is False
    assert result.advertised_total == 9
    assert response.data.complete_count == 0
    assert response.data.partial_count == 1


def test_candidate_batch_can_emit_per_job_jd_acceptance_evidence(tmp_path) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**_kwargs):
        return [
            {
                "id": "complete-job",
                "title": "机器人软件工程师",
                "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/complete",
                "jd_raw": "岗位职责：" + "负责机器人软件开发、系统调试与性能优化。" * 20 + "任职要求：熟悉 Python 和 C++。",
                "capture_evidence": _capture_evidence(
                    "岗位职责：" + "负责机器人软件开发、系统调试与性能优化。" * 20 + "任职要求：熟悉 Python 和 C++。",
                    "https://app.mokahr.com/campus-recruitment/new/1#/jobs/complete",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            },
            {
                "id": "shell-job",
                "title": "算法工程师",
                "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/shell",
                "jd_raw": "岗位职责 任职要求 工作地点",
                "cohort": 2027,
                "cohort_status": "confirmed",
                "recruitment_track": "formal",
            },
        ]

    response = OcCandidateRunner(snapshot, companies, process=process).crawl(
        OcCandidateCrawlBatchInput(
            company_names=["新公司"],
            require_complete_jd=True,
            include_job_evidence=True,
        )
    )

    assert response.data is not None
    result = response.data.results[0]
    assert result.complete_jd_count == 1
    assert result.incomplete_jd_count == 1
    assert result.accepted_count == 1
    assert result.rejection_reasons == {"incomplete_jd": 1}
    assert len(result.job_evidence) == 2
    by_id = {item.source_job_id: item for item in result.job_evidence}
    assert by_id["complete-job"].jd_complete is True
    assert by_id["complete-job"].accepted is True
    assert by_id["shell-job"].jd_complete is False
    assert by_id["shell-job"].accepted is False
    assert by_id["complete-job"].jd_sha256 is not None


@pytest.mark.parametrize("normalize_source", [False, True], ids=["raw_stub", "oc_2027"])
def test_candidate_crawl_distinguishes_missing_jd_from_no_eligible_jobs(
    tmp_path, normalize_source: bool,
) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**kwargs):
        rows = [{
            "id": "missing-jd",
            "title": "软件开发工程师",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/missing-jd",
            "jd_raw": "",
            "cohort": 2027,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }, {
            "id": "old-cohort",
            "title": "软件开发工程师",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/old-cohort",
            "jd_raw": "岗位职责：" + "负责软件开发与测试。" * 30,
            "cohort": 2026,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }]
        if normalize_source:
            campaign = job_cohorts.trusted_source_campaign(
                kwargs["source_context"], jobs_observed=bool(rows),
            )
            assert campaign is not None
            return job_cohorts.annotate_company_jobs(
                rows, kwargs["source_url"], inspect_page=False, campaign=campaign,
            )
        return rows

    runner = OcCandidateRunner(snapshot, companies, process=process)
    _, leads = runner._new_candidates()
    lead = next(item for item in leads if item.canonical_name == "新公司")
    result = runner.crawl_lead(
        lead,
        OcCandidateCrawlBatchInput(require_complete_jd=True, include_job_evidence=True),
    )

    assert result.integration_status == "jd_hydration_required"
    assert result.error_code == "jd_hydration_required"
    # JD validation precedes cohort rejection; authorized 2027 normalization cannot bypass it.
    assert result.rejection_reasons == {"incomplete_jd": 2}
    assert result.accepted_count == result.complete_jd_count == 0
    assert result.incomplete_jd_count == 2
    evidence = {item.source_job_id: item for item in result.job_evidence}
    assert set(evidence) == {"missing-jd", "old-cohort"}
    assert evidence["missing-jd"].cohort == 2027
    assert evidence["old-cohort"].cohort == (2027 if normalize_source else 2026)
    for item in evidence.values():
        assert item.cohort_status == "confirmed"
        assert not item.jd_complete and not item.accepted


def test_candidate_crawl_hydrates_only_otherwise_eligible_jobs(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot, companies = _write_fixture(tmp_path)
    calls = []

    def process(**_kwargs):
        return [{
            "id": "formal-job",
            "title": "软件开发工程师",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/formal-job",
            "jd_raw": "",
            "cohort": 2027,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }, {
            "id": "intern-job",
            "title": "软件开发实习生",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/intern-job",
            "jd_raw": "",
            "cohort": 2027,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }, {
            "id": "doctorate-job",
            "title": "博士后研究员",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/doctorate-job",
            "jd_raw": "",
            "cohort": 2027,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }]

    def fetch_detail(job, timeout_seconds):
        calls.append(job["jd_url"])
        assert job["company"] == "新公司"
        assert job["cohort_status"] == "confirmed"
        assert 0 < timeout_seconds <= 45
        detail = (
            "岗位职责："
            + "负责软件系统开发、测试、性能优化和工程交付。" * 20
            + "任职要求：熟悉 Python。"
        )
        return {
            "detail": detail,
            "status": "complete",
            "identity_status": "matched",
            "capture_evidence": _capture_evidence(
                detail,
                job["jd_url"],
            ),
        }

    monkeypatch.setattr(
        oc_candidates_module,
        "_hydrate_candidate_detail",
        fetch_detail,
    )
    runner = OcCandidateRunner(snapshot, companies, process=process)
    _, leads = runner._new_candidates()
    lead = next(item for item in leads if item.canonical_name == "新公司")
    result = runner.crawl_lead(
        lead,
        OcCandidateCrawlBatchInput(require_complete_jd=True),
        hydrate_details=True,
    )

    assert result.integration_status == "connected_complete"
    assert result.accepted_count == 1
    assert result.complete_jd_count == 1
    assert calls == [
        "https://app.mokahr.com/campus-recruitment/new/1#/jobs/formal-job"
    ]


def test_candidate_crawl_does_not_label_ineligible_job_as_waiting_for_jd(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot, companies = _write_fixture(tmp_path)

    def process(**_kwargs):
        return [{
            "id": "wrong-cohort",
            "title": "社会招聘软件工程师",
            "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/jobs/wrong-cohort",
            "jd_raw": "",
            "cohort": 2026,
            "cohort_status": "confirmed",
            "recruitment_track": "formal",
        }]

    monkeypatch.setattr(
        oc_candidates_module,
        "_hydrate_candidate_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ineligible jobs must not be hydrated")
        ),
    )
    runner = OcCandidateRunner(snapshot, companies, process=process)
    _, leads = runner._new_candidates()
    lead = next(item for item in leads if item.canonical_name == "新公司")
    result = runner.crawl_lead(
        lead,
        OcCandidateCrawlBatchInput(require_complete_jd=True),
        hydrate_details=True,
    )

    assert result.integration_status == "no_eligible_jobs"
    assert result.error_code == "no_eligible_jobs"


@pytest.mark.parametrize("status", ["identity_mismatch", "identity_ambiguous", "timeout", "content_incomplete"])
def test_candidate_hydration_never_accepts_text_from_failed_identity_or_fetch(tmp_path, monkeypatch, status):
    snapshot, companies = _write_fixture(tmp_path)
    raw = {"id": "one", "title": "软件开发工程师", "jd_raw": "",
           "jd_url": "https://app.mokahr.com/campus-recruitment/new/1#/job/one",
           "cohort": 2027, "cohort_status": "confirmed", "recruitment_track": "formal"}
    body = "岗位职责：" + "负责软件开发与系统测试。" * 30 + "任职要求：熟悉 Python。"
    monkeypatch.setattr(oc_candidates_module, "_hydrate_candidate_detail", lambda *_: {"detail": body, "status": status})
    runner = OcCandidateRunner(snapshot, companies, process=lambda **_: [raw.copy()])
    _, leads = runner._new_candidates()
    result = runner.crawl_lead(next(lead for lead in leads if lead.canonical_name == "新公司"),
                              OcCandidateCrawlBatchInput(require_complete_jd=True), hydrate_details=True)
    assert result.accepted_count == result.complete_jd_count == 0
    assert result.error_code == "jd_hydration_required"
    assert f"jd_hydration_{status}:1" in result.termination_reasons


def test_candidate_detail_uses_the_shared_isolated_worker_and_reports_timeout(monkeypatch):
    from packages.pipeline import isolation

    captured = []
    monkeypatch.setattr(isolation, "fetch_job_detail_result_isolated", lambda job, **kw: captured.append((job, kw)) or {"status": "complete", "detail": "text"})
    assert oc_candidates_module._hydrate_candidate_detail({"title": "Engineer"}, 12)["detail"] == "text"
    assert captured == [({"title": "Engineer"}, {"timeout_seconds": 12})]

    def timed_out(*_args, **_kwargs):
        raise isolation.IsolatedOperationTimeout("bounded worker timeout")

    monkeypatch.setattr(isolation, "fetch_job_detail_result_isolated", timed_out)
    assert oc_candidates_module._hydrate_candidate_detail({}, 12) == {
        "detail": "", "status": "timeout", "error_type": "IsolatedOperationTimeout",
    }


@pytest.mark.parametrize("complete", [False, True])
def test_candidate_empty_activity_needs_terminal_evidence(complete):
    url = "https://app.mokahr.com/campus-recruitment/new/1#/jobs"
    _, code, _ = oc_candidates_module._empty_result_status(url, process_result={
        "advertised_total": 0, "pagination_complete": complete,
        "completeness_known": complete, "has_more": not complete,
    })
    assert code == ("activity_empty" if complete else "adapter_variant_unsupported")
