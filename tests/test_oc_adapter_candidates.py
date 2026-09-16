from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.tools.oc_adapter_candidates import (
    AdapterCandidateSpec,
    AdapterFixture,
    accept_adapter_candidate,
)
from packages.tools.oc_candidates import diagnose_candidate_entry


FIXTURES = Path(__file__).parent / "fixtures" / "oc_adapter_candidates"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_entry_diagnosis_separates_reuse_candidates_and_discovery() -> None:
    ats = diagnose_candidate_entry("https://acme.zhiye.com/campus/jobs")
    declarative = diagnose_candidate_entry("https://careers.example.test/campus/jobs")
    python = diagnose_candidate_entry("https://careers.example.test/campus-recruitment")
    discovery = diagnose_candidate_entry("https://www.example.test/")
    invalid = diagnose_candidate_entry("https://mp.weixin.qq.com/s/example")

    assert (ats.entry_kind, ats.crawler_key, ats.candidate_kind) == (
        "existing_adapter", "beisen", "reuse"
    )
    assert declarative.entry_kind == "declarative_candidate"
    assert declarative.candidate_kind == "declarative"
    assert python.entry_kind == "declarative_candidate"
    assert python.candidate_kind == "declarative"
    assert discovery.entry_kind == "entry_discovery_required"
    assert invalid.entry_kind == "invalid_entry"


def test_entry_diagnosis_recognizes_verified_zyt_and_tonghuashun_adapters() -> None:
    zyt = diagnose_candidate_entry("https://we.zyt.com/5/jobs")
    tonghuashun = diagnose_candidate_entry(
        "https://campus.10jqka.com.cn/job/list?type=school&sid=1"
    )

    assert (zyt.entry_kind, zyt.crawler_key, zyt.candidate_kind) == (
        "existing_adapter", "beisen", "reuse"
    )
    assert (
        tonghuashun.entry_kind,
        tonghuashun.crawler_key,
        tonghuashun.candidate_kind,
    ) == ("existing_adapter", "tonghuashun", "reuse")


def test_declarative_candidate_passes_frozen_fixture_and_stops_before_enablement() -> None:
    fixture = AdapterFixture(
        name="api-page-1",
        input=_load("declarative_api.json"),
        expected_titles=["机器人软件工程师", "嵌入式开发工程师"],
    )
    spec = AdapterCandidateSpec(
        company="声明式测试公司",
        source_url="https://careers.example.test/campus/jobs",
        kind="declarative",
        recipe={
            "type": "api_campaigns",
            "request": {
                "method": "POST",
                "url": "https://careers.example.test/api/jobs",
                "body": {"page": 1, "pageSize": 2},
            },
            "pagination": {"page_key": "page", "size_key": "pageSize", "page_size": 2},
            "items_path": "$.data.jobs",
            "total_path": "$.data.total",
            "field_map": {
                "id": "id",
                "title": "title",
                "city": "city",
                "jd": ["description"],
            },
            "detail_url_template": "https://careers.example.test/jobs/{id}",
            "scopes": [
                {
                    "include": True,
                    "label": "2027届校园招聘",
                    "evidence": "2027届校园招聘",
                    "cohort": 2027,
                }
            ],
        },
        fixtures=[fixture],
    )

    result = accept_adapter_candidate(spec, candidate_root=FIXTURES)

    assert result.state == "awaiting_approval"
    assert result.runtime_enabled is False
    assert result.fixture_results[0].passed is True
    assert result.fixture_results[0].accepted_count == 2


def test_python_candidate_runs_in_worker_and_binds_source_hash() -> None:
    spec = AdapterCandidateSpec(
        company="Python测试公司",
        source_url="https://careers.example.test/jobs",
        kind="python",
        python_file="python_candidate.py",
        fixtures=[AdapterFixture(
            name="python-parser",
            input=_load("python_fixture.json"),
            expected_titles=["感知算法工程师"],
        )],
    )

    first = accept_adapter_candidate(spec, candidate_root=FIXTURES)
    second = accept_adapter_candidate(spec, candidate_root=FIXTURES)

    assert first.state == "awaiting_approval"
    assert first.runtime_enabled is False
    assert first.source_sha256 is not None
    assert first.candidate_id == second.candidate_id
    assert first.fixture_results[0].accepted_count == 1


def test_title_mismatch_fails_closed() -> None:
    spec = AdapterCandidateSpec(
        company="Python测试公司",
        source_url="https://careers.example.test/jobs",
        kind="python",
        python_file="python_candidate.py",
        fixtures=[AdapterFixture(
            name="wrong-expectation",
            input=_load("python_fixture.json"),
            expected_titles=["不存在的岗位"],
        )],
    )

    result = accept_adapter_candidate(spec, candidate_root=FIXTURES)

    assert result.state == "fixture_failed"
    assert result.runtime_enabled is False
    assert result.fixture_results[0].error_code == "fixture_title_mismatch"


def test_python_candidate_cannot_escape_candidate_root(tmp_path: Path) -> None:
    spec = AdapterCandidateSpec(
        company="越界测试公司",
        source_url="https://careers.example.test/jobs",
        kind="python",
        python_file=str(FIXTURES / "python_candidate.py"),
        fixtures=[AdapterFixture(
            name="fixture",
            input=_load("python_fixture.json"),
            expected_titles=["感知算法工程师"],
        )],
    )

    with pytest.raises(ValueError, match="candidate_root"):
        accept_adapter_candidate(spec, candidate_root=tmp_path)


def test_declarative_delegate_cannot_escape_fixture_transport() -> None:
    with pytest.raises(ValidationError, match="api_campaigns, html_list, or dom"):
        AdapterCandidateSpec(
            company="委托测试公司",
            source_url="https://acme.zhiye.com/campus/jobs",
            kind="declarative",
            recipe={
                "type": "delegate",
                "crawler": "beisen",
                "url": "https://acme.zhiye.com/campus/jobs",
            },
            fixtures=[AdapterFixture(
                name="offline-only",
                input={},
                expected_titles=["测试岗位"],
            )],
        )
