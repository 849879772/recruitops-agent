from __future__ import annotations

import importlib
import re
from pathlib import Path

import yaml

from packages.recruitment_core import (
    CRAWLER_MAP,
    CompanyConfig,
    crawl_company,
    crawl_company_with_evidence,
)
from packages.recruitment_core import job_cohorts


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT / "packages" / "recruitment_core"


def test_every_registered_crawler_is_importable_from_the_standalone_package() -> None:
    assert CRAWLER_MAP
    for key, crawler_class in CRAWLER_MAP.items():
        assert crawler_class.__module__.startswith("packages.recruitment_core.crawlers"), key
        module = importlib.import_module(crawler_class.__module__)
        assert getattr(module, crawler_class.__name__) is crawler_class


def test_generic_platform_crawlers_can_be_constructed_without_network() -> None:
    for key in ("moka", "beisen", "feishu"):
        crawler = CRAWLER_MAP[key]("Example Company", "https://jobs.example.test/campus")
        assert crawler.company_name == "Example Company"
        assert crawler.careers_url == "https://jobs.example.test/campus"


def test_crawl_company_accepts_a_legacy_company_dict() -> None:
    calls: list[tuple[str, str]] = []

    class FakeCrawler:
        def __init__(self, company_name: str, careers_url: str) -> None:
            calls.append((company_name, careers_url))

        def fetch(self) -> list[dict[str, str]]:
            return [
                {
                    "title": "Software Engineer",
                    "city": "Shanghai",
                    "jd_url": "https://jobs.example.test/job/1",
                }
            ]

    jobs = crawl_company(
        {
            "name": "Example Company",
            "careers_url": "https://jobs.example.test/campus",
            "crawler": "fake",
            "campaign_text": "2027届校园招聘",
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert jobs[0]["title"] == "Software Engineer"
    assert calls == [("Example Company", "https://jobs.example.test/campus")]


def test_company_scope_and_detail_interactions_follow_list_jobs() -> None:
    class FakeCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.careers_url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [{
                "title": "具身 AI Infra 研发工程师",
                "city": "北京",
                "jd_url": self.careers_url,
                "link_kind": "list",
            }]

    recipe = {
        "mode": "inline",
        "trigger_selector": ".job-card",
        "container_selector": ".job-card",
    }
    jobs = crawl_company(
        {
            "name": "灵心巧手",
            "careers_url": "https://www.linkerbot.cn/about/join/",
            "crawler": "fake",
            "campaign_text": "2027届校园招聘",
            "entry_click_texts": ["校园招聘"],
            "detail_interaction": recipe,
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert jobs[0]["entry_click_texts"] == ["校园招聘"]
    assert jobs[0]["detail_interaction"] == recipe


def test_gongji_uses_identity_bound_drawer_detail_recipe() -> None:
    config = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text(encoding="utf-8"))
    company = next(item for item in config["companies"] if item["name"] == "共济科技")

    assert company["detail_interaction"] == {
        "mode": "dialog",
        "trigger_selector": ".ant-card a",
        "trigger_text": "查看详情",
        "container_selector": ".ant-drawer-content",
        "bind_job_id": False,
    }


def test_rich_crawler_result_preserves_unknown_and_explicit_incomplete_evidence() -> None:
    class UnknownCrawler:
        def __init__(self, company_name: str, careers_url: str) -> None:
            pass

        def fetch(self) -> list[dict[str, str]]:
            return [{"title": "Software Engineer", "jd_url": "https://jobs.example.test/1"}]

    unknown = crawl_company_with_evidence(
        {"name": "Example", "careers_url": "https://jobs.example.test", "crawler": "fake"},
        crawler_map={"fake": UnknownCrawler},
    )
    assert unknown["completeness_known"] is False
    assert unknown["pagination_complete"] is False
    assert unknown["jobs"]

    class PartialCrawler(UnknownCrawler):
        pagination_complete = False
        pages_seen = 1
        total_pages = 3
        has_more = True
        pagination_termination_reason = "next_navigation_failed"

    partial = crawl_company_with_evidence(
        {"name": "Example", "careers_url": "https://jobs.example.test", "crawler": "fake"},
        crawler_map={"fake": PartialCrawler},
    )
    assert partial["jobs"]
    assert partial["pagination_complete"] is False
    assert partial["pages_seen"] == 1
    assert partial["total_pages"] == 3
    assert crawl_company(
        {"name": "Example", "careers_url": "https://jobs.example.test", "crawler": "fake"},
        crawler_map={"fake": PartialCrawler},
    ) == []


def test_list_page_jobs_are_deduplicated_by_title_not_shared_url() -> None:
    class ListCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [
                {"title": "算法工程师", "city": "上海", "jd_url": self.url, "link_kind": "list"},
                {"title": "软件工程师", "city": "上海", "jd_url": self.url, "link_kind": "list"},
            ]

    jobs = crawl_company(
        {
            "name": "Example",
            "careers_url": "https://jobs.example.test/list",
            "crawler": "fake",
            "campaign_text": "2027届校园招聘",
        },
        crawler_map={"fake": ListCrawler},
    )

    assert [job["title"] for job in jobs] == ["算法工程师", "软件工程师"]
    assert len(jobs) == 2


def test_core_attaches_recruitment_track_and_campaign_cohort_evidence() -> None:
    class FakeCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.company_name = company_name
            self.careers_url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [
                {
                    "title": "C++ 软件开发工程师",
                    "city": "上海",
                    "job_type": "校招",
                    "jd_url": "https://jobs.example.test/1",
                    "jd_raw": "职位描述 任职要求 C++ Linux",
                },
                {
                    "title": "算法实习生",
                    "city": "北京",
                    "job_type": "实习",
                    "jd_url": "https://jobs.example.test/2",
                    "jd_raw": "实习生招聘",
                },
            ]

    jobs = crawl_company(
        {
            "name": "示例公司",
            "careers_url": "https://jobs.example.test/campus",
            "crawler": "fake",
            "campaign_text": "2027届校园招聘",
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert jobs[0]["recruitment_track"] == "formal"
    assert jobs[0]["cohort"] == 2027
    assert jobs[0]["cohort_status"] == "confirmed"
    assert jobs[1]["recruitment_track"] == "internship"


def test_oc_address_with_observed_jobs_is_authoritative_cohort_evidence(monkeypatch) -> None:
    class FakeCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.company_name = company_name
            self.careers_url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [
                {
                    "title": "机器人软件工程师",
                    "job_type": "校招",
                    "jd_url": "https://sharpa.jobs.feishu.cn/668262/position/1/detail",
                    "jd_raw": "负责机器人软件研发。",
                },
                {
                    "title": "机器人算法实习生",
                    "job_type": "实习",
                    "jd_url": "https://sharpa.jobs.feishu.cn/668262/position/2/detail",
                    "jd_raw": "机器人算法实习生招聘。",
                },
            ]

    monkeypatch.setattr(
        job_cohorts,
        "inspect_official_campaign",
        lambda url: {
            **job_cohorts.unknown_cohort(campaign_url=url),
            "official_campus_portal": True,
            "portal_evidence": "Sharpa Robotics校招官网",
        },
    )
    jobs = crawl_company(
        {
            "name": "Sharpa Robotics",
            "careers_url": "https://sharpa.jobs.feishu.cn/668262/",
            "crawler": "fake",
            "source_cohort": 2027,
            "source_cohort_source": job_cohorts.OC_TRUSTED_SOURCE,
            "source_cohort_evidence": "OC固定筛选记录：招聘对象=2027届；招聘类型=秋招提前批",
            "source_cohort_url": "https://www.givemeoc.com/",
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert {job["cohort"] for job in jobs} == {2027}
    assert {job["cohort_status"] for job in jobs} == {"confirmed"}
    assert jobs[0]["cohort_source"] == "OC 2027届秋招授权来源"
    assert "OC提供地址已实际提取岗位" in jobs[0]["cohort_evidence"]
    assert jobs[0]["recruitment_track"] == "formal"
    assert jobs[1]["recruitment_track"] == "internship"


def test_oc_requires_observed_jobs_but_overrides_destination_cohort_conflict() -> None:
    config = {
        "source_cohort": 2027,
        "source_cohort_source": job_cohorts.OC_TRUSTED_SOURCE,
        "source_cohort_evidence": "OC固定筛选记录：招聘对象=2027届；招聘类型=秋招",
        "source_cohort_url": "https://jobs.example.test/",
    }
    no_portal = job_cohorts.unknown_cohort(campaign_url=config["source_cohort_url"])
    conflict = {
        **no_portal,
        "cohort": 2026,
        "cohort_status": "confirmed",
        "official_campus_portal": True,
        "portal_evidence": "示例公司2026届校园招聘",
    }

    assert job_cohorts.trusted_source_campaign(config, no_portal) is None
    trusted = job_cohorts.trusted_source_campaign(
        config,
        conflict,
        jobs_observed=True,
    )
    assert trusted is not None
    assert trusted["cohort"] == 2027
    assert trusted["cohort_status"] == "confirmed"
    assert trusted["campaign_scope"] == "trusted_source_override"


def test_oc_authorization_overrides_explicit_old_year_on_observed_job() -> None:
    class FakeCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.company_name = company_name
            self.careers_url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [{
                "title": "软件工程师",
                "job_type": "2026届校园招聘",
                "jd_url": "https://jobs.example.test/1",
                "jd_raw": "负责软件开发。",
            }]

    jobs = crawl_company(
        {
            "name": "OC示例公司",
            "careers_url": "https://jobs.example.test/",
            "crawler": "fake",
            "source_cohort": 2027,
            "source_cohort_source": job_cohorts.OC_TRUSTED_SOURCE,
            "source_cohort_evidence": "OC固定筛选记录：招聘对象=2027届；招聘类型=秋招",
            "source_cohort_url": "https://jobs.example.test/",
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert jobs[0]["cohort"] == 2027
    assert jobs[0]["cohort_status"] == "confirmed"
    assert jobs[0]["cohort_source"] == "OC 2027届秋招授权来源"


def test_oc_snapshot_config_overrides_explicit_destination_year() -> None:
    class FakeCrawler:
        pagination_complete = True

        def __init__(self, company_name: str, careers_url: str) -> None:
            self.company_name = company_name
            self.careers_url = careers_url

        def fetch(self) -> list[dict[str, str]]:
            return [{
                "title": "2026届软件工程师",
                "job_type": "2026届校园招聘",
                "jd_url": "https://jobs.example.test/1",
                "jd_raw": "负责软件开发。",
            }]

    jobs = crawl_company(
        {
            "name": "OC配置示例公司",
            "careers_url": "https://jobs.example.test/",
            "crawler": "fake",
            "discovery_source": "oc_snapshot",
            "recruitment_targets": ["2027届"],
        },
        crawler_map={"fake": FakeCrawler},
    )

    assert jobs[0]["cohort"] == 2027
    assert jobs[0]["cohort_status"] == "confirmed"
    assert jobs[0]["cohort_source"] == "OC 2027届秋招授权来源"
    assert "OC提供地址已实际提取岗位" in jobs[0]["cohort_evidence"]


def test_known_dedicated_campus_routes_supply_official_portal_evidence() -> None:
    urls = [
        "https://talent.baidu.com/jobs/",
        "https://campus.163.com/app/job/position?id=103",
        "https://jobs.mihoyo.com/#/campus/position",
        "https://career.huawei.com/cn/campus-recruitment",
        "https://join.qq.com/post.html",
    ]

    for url in urls:
        confirmed, evidence = job_cohorts._portal_evidence("", url)
        assert confirmed is True
        assert evidence.startswith("官方招聘平台校园招聘路由")


def test_company_config_round_trips_legacy_fields() -> None:
    config = CompanyConfig.from_legacy(
        {
            "name": "Example Company",
            "careers_url": "https://jobs.example.test/campus",
            "crawler": "moka",
            "campaign_urls": "https://jobs.example.test/campaign",
            "aliases": ["Example"],
            "custom_option": "kept",
        }
    )

    assert config.campaign_urls == ("https://jobs.example.test/campaign",)
    assert config.aliases == ("Example",)
    assert config.to_dict()["custom_option"] == "kept"


def test_standalone_sources_have_no_legacy_runtime_imports() -> None:
    source_paths = [*CORE_ROOT.rglob("*.py"), ROOT / "scripts" / "run_agent_crawler.py"]
    forbidden_path = "D:" + "/秋招系统"
    forbidden_import = re.compile(
        r"(?m)^\s*(?:from|import)\s+"
        r"(?:crawlers|job_filters|job_cohorts|job_details)(?:\s|$)"
    )

    for path in source_paths:
        source = path.read_text(encoding="utf-8")
        assert forbidden_path not in source, path
        assert "D:" + "\\秋招系统" not in source, path
        assert forbidden_import.search(source) is None, path
        assert "sys" + ".path" not in source, path
