import json

import pytest

from scripts import run_offerbiu_full_crawl as module


def row(key, url="https://example.com/campus"):
    return {"id": key, "companyId": "company-1", "companyName": "Example",
            "applyUrl": url, "targetYears": [2027], "recruitType": "秋招",
            "industryGroupCodes": ["internet-tech"]}


def test_plan_preserves_all_records_and_all_distinct_entries():
    tasks, excluded = module.plan({"source": "offerbiu", "items": [row(1), row(2), row(3, ["https://example.com/a", "https://example.com/b"])]})
    assert len(tasks) == 3
    assert len(tasks[0]["sources"]) == 2
    assert not excluded


def test_full_run_resume_and_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps({"source": "offerbiu", "items": [row(1), row(2, "https://mp.weixin.qq.com/s/a")]}), encoding="utf-8")
    calls = []

    def evaluate(value, **kwargs):
        calls.append(value)
        return {"company": value["companyName"], "crawler_status": "raw_jobs_observed",
                "crawl": {"raw_job_count": 1, "raw_jobs": [{"title": "Engineer"}],
                          "pagination_evidence": {"pagination_complete": False}}}

    output = tmp_path / ".data/evals/run"
    summary = module.run(source, output, evaluate=evaluate)
    assert summary["scope_complete"] is True
    assert summary["pagination_complete_entries"] == 0
    assert summary["raw_job_rows"] == 1
    assert len(calls) == 1
    module.run(source, output, resume=True, evaluate=evaluate)
    assert len(calls) == 1
    assert len((output / "raw-jobs.jsonl").read_text().splitlines()) == 1
    with pytest.raises(ValueError, match="requires"):
        module.run(source, output, evaluate=evaluate)


def test_output_guard(tmp_path):
    with pytest.raises(ValueError, match="Output"):
        module.run(tmp_path / "missing", tmp_path / "outside")
