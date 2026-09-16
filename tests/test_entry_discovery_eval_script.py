from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from packages.discovery.public_entries import PublicSearchHit, RankedEntryCandidate
from scripts.run_entry_discovery_eval import run_evaluation


def _write_fixture(path: Path) -> None:
    path.write_text(json.dumps({
        "fixture_version": "fixture-v1",
        "companies": [
            {
                "id": "adapter",
                "name": "已有飞书",
                "cohort": "debug",
                "careers_url": "https://brand.jobs.feishu.cn/123/",
            },
            {
                "id": "homepage",
                "name": "待找入口",
                "cohort": "debug",
                "careers_url": "https://www.example.com/",
            },
            {
                "id": "form",
                "name": "问卷",
                "cohort": "acceptance",
                "careers_url": "https://www.wenjuan.com/s/abc/",
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _write_outcome_fixture(path: Path) -> None:
    path.write_text(json.dumps({
        "fixture_version": "outcomes-v1",
        "companies": [
            {
                "id": "complete",
                "name": "完整岗位",
                "cohort": "debug",
                "careers_url": "https://complete.jobs.feishu.cn/1/",
                "crawler": "feishu",
            },
            {
                "id": "partial",
                "name": "部分岗位",
                "cohort": "debug",
                "careers_url": "https://partial.jobs.feishu.cn/2/",
                "crawler": "feishu",
            },
            {
                "id": "empty",
                "name": "无岗位活动",
                "cohort": "debug",
                "careers_url": "https://empty.jobs.feishu.cn/3/",
                "crawler": "feishu",
            },
            {
                "id": "blocked",
                "name": "访问受限",
                "cohort": "debug",
                "careers_url": "https://blocked.jobs.feishu.cn/4/",
                "crawler": "feishu",
            },
            {
                "id": "not-found",
                "name": "入口缺失",
                "cohort": "debug",
                "careers_url": "https://not-found.jobs.feishu.cn/5/",
                "crawler": "feishu",
            },
            {
                "id": "failed",
                "name": "爬取失败",
                "cohort": "debug",
                "careers_url": "https://failed.jobs.feishu.cn/6/",
                "crawler": "feishu",
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _crawl_evidence(
    jobs: list[dict] | None = None,
    *,
    pagination_complete: bool | None = True,
    completeness_known: bool = True,
    pages_seen: int = 1,
    total_pages: int | None = 1,
    has_more: bool = False,
    advertised_total: int | None = None,
    error_code: str | None = None,
    run_reason: str | None = None,
    failures: list[str] | None = None,
) -> dict:
    result = {
        "jobs": jobs if jobs is not None else [{"title": "Software Engineer"}],
        "pagination_complete": pagination_complete,
        "completeness_known": completeness_known,
        "pages_seen": pages_seen,
        "total_pages": total_pages,
        "has_more": has_more,
        "advertised_total": advertised_total,
        "termination_reasons": ["fixture_complete"],
        "failures": failures or [],
    }
    if error_code is not None:
        result["error_code"] = error_code
    if run_reason is not None:
        result["run_reason"] = run_reason
    return result


def test_eval_routes_only_true_homepage_to_search(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture)
    calls: list[str] = []
    crawled: list[tuple[str, str, str]] = []

    def search(name: str, **_kwargs):
        calls.append(name)
        hit = PublicSearchHit(
            provider="fixture",
            query=f"{name} 校园招聘",
            url="https://candidate.example.com/campus/jobs",
            title=f"{name} 2027校园招聘",
            snippet="招聘岗位",
        )
        return [hit.query], [RankedEntryCandidate(
            hit=hit,
            score=75,
            entry_kind="declarative_candidate",
            crawler_key="render",
            company_evidence=name,
        )]

    def crawl(company: dict, *, timeout_seconds: float):
        crawled.append((company["name"], company["careers_url"], company["crawler"]))
        return _crawl_evidence()

    report = run_evaluation(fixture, search=search, crawl=crawl)

    assert calls == ["待找入口"]
    assert crawled == [
        ("已有飞书", "https://brand.jobs.feishu.cn/123/", "feishu"),
        ("待找入口", "https://candidate.example.com/campus/jobs", "render"),
    ]
    assert report["summary"] == {
        "selected": 3,
        "actions": {
            "excluded_form": 1,
            "search_candidates": 1,
            "validate_existing_url": 1,
        },
        "statuses": {
            "jobs_complete": 2,
            "jobs_partial": 0,
            "activity_empty": 0,
            "access_blocked": 0,
            "entry_not_found": 1,
            "crawl_failed": 0,
        },
        "candidate_count": 1,
        "crawl_attempt_count": 2,
        "search_error_count": 0,
    }
    assert report["writes"] == {"config": 0, "database": 0}
    candidate = report["results"][1]["candidates"][0]
    assert candidate["verification_status"] == "verified"
    assert candidate["url"] == "https://candidate.example.com/campus/jobs"
    assert candidate["crawler_key"] == "render"
    assert candidate["job_count"] == 1
    assert candidate["pagination_evidence"] == {
        "pagination_complete": True,
        "completeness_known": True,
        "pagination_state": "complete",
        "pages_seen": 1,
        "total_pages": 1,
        "has_more": False,
        "advertised_total": None,
        "termination_reasons": ["fixture_complete"],
    }
    assert report["results"][2]["status"] == "entry_not_found"


def test_eval_cohort_and_limit_are_deterministic(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture)

    report = run_evaluation(
        fixture,
        cohort="acceptance",
        limit=1,
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no search")),
    )

    assert report["summary"]["selected"] == 1
    assert report["results"][0]["id"] == "form"
    assert report["results"][0]["action"] == "excluded_form"


def test_eval_records_search_failure_per_company(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture)

    report = run_evaluation(
        fixture,
        cohort="debug",
        search=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        crawl=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crawl offline")),
    )

    failed = next(item for item in report["results"] if item["id"] == "homepage")
    assert failed["status"] == "crawl_failed"
    assert failed["error"] == "RuntimeError: offline"
    assert report["summary"]["search_error_count"] == 1


def test_eval_marks_empty_search_as_entry_not_found_without_crawl(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    _write_fixture(fixture)
    crawl_calls: list[str] = []

    def crawl(company: dict, *, timeout_seconds: float):
        crawl_calls.append(company["name"])
        raise AssertionError("an unresolved entry must not be crawled")

    report = run_evaluation(
        fixture,
        cohort="debug",
        search=lambda *_args, **_kwargs: (["待找入口 校园招聘 官网"], []),
        crawl=crawl,
    )

    unresolved = next(item for item in report["results"] if item["id"] == "homepage")
    assert unresolved["status"] == "entry_not_found"
    assert unresolved["candidates"] == []
    assert unresolved["crawl_attempts"] == []
    assert unresolved["error_code"] == "entry_not_found"
    assert unresolved["crawl_source_url"] is None
    assert crawl_calls == ["已有飞书"]


def test_eval_classifies_all_real_crawl_outcomes(tmp_path: Path) -> None:
    fixture = tmp_path / "outcomes.json"
    _write_outcome_fixture(fixture)

    def crawl(company: dict, *, timeout_seconds: float):
        outcome = company["id"]
        if outcome == "partial":
            return _crawl_evidence(
                pagination_complete=False, pages_seen=1, total_pages=3,
                has_more=True, advertised_total=3, failures=["page_2_timeout"],
            )
        if outcome == "empty":
            return _crawl_evidence(
                jobs=[], advertised_total=0, run_reason="activity_empty",
            )
        if outcome == "blocked":
            return _crawl_evidence(
                jobs=[], pagination_complete=False, completeness_known=False,
                total_pages=None, error_code="login_required",
            )
        if outcome == "not-found":
            return _crawl_evidence(
                jobs=[], pagination_complete=False, completeness_known=False,
                total_pages=None, error_code="adapter_variant_unsupported",
            )
        if outcome == "failed":
            raise TimeoutError("fixture timeout")
        return _crawl_evidence(advertised_total=1)

    report = run_evaluation(fixture, crawl=crawl, workers=2)

    assert [row["id"] for row in report["results"]] == [
        "complete", "partial", "empty", "blocked", "not-found", "failed",
    ]
    assert [row["status"] for row in report["results"]] == [
        "jobs_complete", "jobs_partial", "activity_empty", "access_blocked",
        "entry_not_found", "crawl_failed",
    ]
    assert report["summary"]["statuses"] == {
        "jobs_complete": 1,
        "jobs_partial": 1,
        "activity_empty": 1,
        "access_blocked": 1,
        "entry_not_found": 1,
        "crawl_failed": 1,
    }
    partial = report["results"][1]["crawl_attempts"][0]
    assert partial["url"] == "https://partial.jobs.feishu.cn/2/"
    assert partial["crawler_key"] == "feishu"
    assert partial["job_count"] == 1
    assert partial["pagination_evidence"]["pagination_state"] == "incomplete"
    assert partial["pagination_evidence"]["pages_seen"] == 1
    assert partial["pagination_evidence"]["total_pages"] == 3
    assert partial["pagination_evidence"]["has_more"] is True


def test_eval_bounds_concurrency_keeps_fixture_order_and_isolates_failure(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    companies = [
        {
            "id": f"co-{index}",
            "name": f"Company {index}",
            "cohort": "debug",
            "careers_url": f"https://co-{index}.jobs.feishu.cn/1/",
            "crawler": "feishu",
        }
        for index in range(6)
    ]
    fixture.write_text(json.dumps({"fixture_version": "order-v1", "companies": companies}), encoding="utf-8")
    lock = threading.Lock()
    active = 0
    max_active = 0

    def crawl(company: dict, *, timeout_seconds: float):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            if company["id"] == "co-2":
                raise RuntimeError("one company failed")
            return _crawl_evidence(advertised_total=1)
        finally:
            with lock:
                active -= 1

    report = run_evaluation(fixture, crawl=crawl, workers=2)

    assert max_active <= 2
    assert [row["id"] for row in report["results"]] == [f"co-{index}" for index in range(6)]
    assert report["results"][2]["status"] == "crawl_failed"
    assert [row["status"] for row in report["results"][:2]] == ["jobs_complete", "jobs_complete"]
    assert [row["status"] for row in report["results"][3:]] == [
        "jobs_complete", "jobs_complete", "jobs_complete",
    ]
