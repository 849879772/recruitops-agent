from __future__ import annotations

import json
from pathlib import Path

from evals.rag_live_platform_ab import (
    CrawlProbe,
    LivePlatformCase,
    load_knowledge_fixture,
    load_live_fixture,
    run_live_rag_ab,
)
from packages.matching.models import DeepSeekResponse


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, system_prompt, user_prompt, max_tokens=None):
        del system_prompt, max_tokens
        self.calls.append(user_prompt)
        if "human-resources" in user_prompt:
            selected = "manual_required"
        elif "北森招聘平台" in user_prompt:
            selected = "beisen"
        else:
            selected = "static_html"
        return DeepSeekResponse(
            content=json.dumps({"selected_crawler": selected, "reason": "fixture"}),
            model="fake",
            input_tokens=10,
            cache_creation_input_tokens=2,
            cache_read_input_tokens=3,
            output_tokens=4,
        )


def test_live_fixtures_do_not_embed_company_names_in_knowledge() -> None:
    cases = load_live_fixture()
    knowledge = load_knowledge_fixture()
    corpus = "\n".join(document.content for document in knowledge.documents)

    assert len(cases.cases) == 4
    assert all(case.company not in corpus for case in cases.cases)
    assert {case.expected_crawler for case in cases.cases} == {
        "beisen",
        "manual_required",
    }


def test_diverse_live_fixture_covers_multiple_registered_platforms() -> None:
    fixture_root = Path(__file__).parents[1] / "evals" / "fixtures"
    cases = load_live_fixture(fixture_root / "rag_live_platform_cases_diverse.json")
    knowledge = load_knowledge_fixture(
        fixture_root / "rag_live_platform_knowledge_diverse.json"
    )
    corpus = "\n".join(document.content for document in knowledge.documents)

    assert len(cases.cases) == 3
    assert {case.expected_crawler for case in cases.cases} == {
        "beisen",
        "feishu",
        "hotjob",
    }
    assert all(case.company not in corpus for case in cases.cases)


def test_paired_live_runner_uses_retrieval_only_for_rag_and_real_probe_results() -> None:
    client = FakeClient()
    probes: list[tuple[str, str]] = []

    def runner(case: LivePlatformCase, selected: str) -> CrawlProbe:
        probes.append((case.case_id, selected))
        return CrawlProbe(
            selected_crawler=selected,
            job_count=5 if selected == "beisen" else 0,
            elapsed_ms=20,
            success=selected == "beisen",
        )

    report = run_live_rag_ab(client, crawl_runner=runner)

    assert report.synthetic is False
    assert report.result_type == "live"
    assert report.baseline.success_rate == 0.25
    assert report.rag.success_rate == 1.0
    assert report.rag_minus_baseline["success_rate"] == 0.75
    assert report.recommendation == "retain_for_crawler_routing"
    assert report.baseline.input_tokens == report.rag.input_tokens == 40
    assert report.baseline.total_input_tokens == report.rag.total_input_tokens == 60
    assert all(not item.retrieval_source_refs for item in report.baseline.results)
    assert all(item.retrieval_source_refs for item in report.rag.results)
    assert len(probes) == 7


def test_equal_live_results_recommend_against_formal_routing() -> None:
    class AlwaysCorrectClient(FakeClient):
        def complete(self, *, system_prompt, user_prompt, max_tokens=None):
            selected = "manual_required" if "human-resources" in user_prompt else "beisen"
            return DeepSeekResponse(
                content=json.dumps({"selected_crawler": selected, "reason": "correct"}),
                input_tokens=8,
                output_tokens=3,
            )

    def runner(case: LivePlatformCase, selected: str) -> CrawlProbe:
        return CrawlProbe(
            selected_crawler=selected,
            job_count=3 if selected == "beisen" else 0,
            success=selected == "beisen",
        )

    report = run_live_rag_ab(AlwaysCorrectClient(), crawl_runner=runner)

    assert report.baseline.success_rate == report.rag.success_rate == 1.0
    assert report.recommendation == "do_not_add_to_formal_crawler_routing"
