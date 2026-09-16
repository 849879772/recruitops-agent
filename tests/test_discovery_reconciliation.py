from __future__ import annotations

import copy

from packages.discovery import (
    SourceLead,
    consolidate_source_leads,
    normalize_company_name,
    reconcile_companies,
    source_identity_for_url,
)


def _lead(name: str, **kwargs) -> SourceLead:
    return SourceLead(canonical_name=name, source="oc_snapshot", **kwargs)


def test_company_name_normalization_is_exact_without_fuzzy_suffix_removal() -> None:
    assert normalize_company_name("  ACME，科技 ") == "acme科技"
    assert normalize_company_name("ACME 科技") == "acme科技"
    assert normalize_company_name("ACME科技有限公司") != normalize_company_name("ACME科技")


def test_reconciliation_matches_name_alias_and_unique_source_identity() -> None:
    companies = [
        {
            "id": "c1",
            "name": "甲科技",
            "aliases": ["甲科技集团"],
            "careers_url": "https://jia.jobs.feishu.cn/campus/position",
        },
        {
            "id": "c2",
            "name": "乙公司",
            "careers_url": "https://yi.example/campus/jobs",
        },
    ]
    leads = [
        _lead("甲科技集团"),
        _lead("未命名公司", source_urls=("https://jia.jobs.feishu.cn/s/abc",)),
        _lead("丙公司"),
    ]

    result = reconcile_companies(leads, companies)

    assert [lead.matched_company for lead in result.existing] == ["甲科技", "甲科技"]
    assert [lead.canonical_name for lead in result.new] == ["丙公司"]
    assert result.ambiguous == ()


def test_reconciliation_reports_alias_collisions_and_name_identity_disagreement() -> None:
    companies = [
        {
            "id": "c1",
            "name": "甲公司",
            "aliases": ["共同别名"],
            "careers_url": "https://jia.example/jobs",
        },
        {
            "id": "c2",
            "name": "乙公司",
            "aliases": ["共同别名"],
            "careers_url": "https://yi.example/jobs",
        },
    ]
    leads = [
        _lead("共同别名"),
        _lead("甲公司", source_urls=("https://yi.example/jobs",)),
    ]

    result = reconcile_companies(leads, companies)

    assert result.existing == ()
    assert [lead.canonical_name for lead in result.ambiguous] == ["共同别名", "甲公司"]
    assert all(result.ambiguous_reasons.values())


def test_source_identity_is_platform_specific_and_reconciliation_is_read_only() -> None:
    assert source_identity_for_url(
        "https://campus.example.zhiye.com/campus/jobs"
    ) != source_identity_for_url("https://campus.example.zhiye.com/5/jobs")
    assert source_identity_for_url(
        "https://a.jobs.feishu.cn/s/one"
    ) == source_identity_for_url("https://a.jobs.feishu.cn/campus/position")
    assert source_identity_for_url(
        "https://custom.example/campus-recruitment/acme/100#/home"
    ).startswith("moka:acme")

    companies = [{
        "id": "c1",
        "name": "甲公司",
        "aliases": ["甲科技"],
        "careers_url": "https://a.jobs.feishu.cn/campus/position",
    }]
    before = copy.deepcopy(companies)
    result = reconcile_companies([_lead("新名字", source_urls=("https://a.jobs.feishu.cn/s/one",))], companies)

    assert result.counts == {"existing": 1, "new": 0, "ambiguous": 0}
    assert companies == before


def test_reconciliation_merges_explicit_recruitment_project_into_parent_company() -> None:
    lead = _lead(
        "vivo-产品总经理储备计划",
        source_urls=("https://hr-campus.vivo.com/campus/project/pm",),
        metadata={"recruitment_targets": ["2027届"]},
    )

    result = reconcile_companies(
        [lead],
        [{"name": "vivo", "careers_url": "https://hr-campus.vivo.com/campus/jobs"}],
    )

    assert len(result.existing) == 1
    assert result.existing[0].matched_company == "vivo"
    assert result.existing[0].metadata["source_project_name"] == "vivo-产品总经理储备计划"
    assert result.existing[0].metadata["source_project_parent"] == "vivo"


def test_reconciliation_does_not_merge_plain_prefix_similarity() -> None:
    lead = _lead("vivo手机配件科技")

    result = reconcile_companies([lead], [{"name": "vivo"}])

    assert result.new == (lead,)


def test_consolidate_source_leads_merges_projects_only_by_shared_source() -> None:
    shared = "HTTPS://career.huawei.com:443/reccampportal/portal5/campus-recruitment.html?x=1&amp;y=2"
    leads = [
        _lead("华为-软件精英挑战赛项目", source_urls=(shared,), metadata={"source_rows": 2}),
        _lead("华为-勇敢新世界项目", source_urls=(shared.replace("HTTPS", "https"),)),
        _lead("华为不同入口", source_urls=("https://career.huawei.com/another/jobs",)),
    ]

    result = consolidate_source_leads(leads)

    assert len(result) == 2
    merged = next(lead for lead in result if lead.metadata.get("source_project_count") == 2)
    assert set(merged.metadata["source_project_names"]) == {
        "华为-软件精英挑战赛项目",
        "华为-勇敢新世界项目",
    }
    assert merged.metadata["source_rows"] == 3
    assert merged.source_urls == (
        "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html?x=1&y=2",
    )
