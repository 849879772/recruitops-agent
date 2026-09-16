from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

from packages.pipeline import daily
from packages.recruitment_core.jd_capture import assess_jd_capture
from scripts import hydrate_offerbiu_full_crawl as capture


URL = "https://jobs.bytedance.com/campus/position/7667551275686594821/detail"
POST_ID = "7667551275686594821"


def _job(index):
    return {
        "id": f"local-{index}", "company_id": f"co-{index}",
        "company": f"Source {index}", "title": "C++ Engineer",
        "detail_url": URL, "jd_url": URL, "source_job_id": POST_ID,
        "cohort": 2027, "cohort_status": "confirmed", "job_type": "campus",
        "jd_raw": "unverified original", "capture_evidence": {},
    }


def _reply():
    detail = "C++ development on Linux."
    return {
        "status": "complete", "detail": detail, "detail_url": URL,
        "source": "feishu_api", "identity_status": "request_bound",
        "identity_evidence": [f"native_id:{POST_ID}", "title:C++ Engineer"],
        "capture_evidence": {
            "status": "complete", "method": "official_api", "source_url": URL,
            "content_sha256": sha256(detail.encode()).hexdigest(),
            "identity_verified": True, "terminal_observed": True,
            "remaining_controls": [],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def test_daily_reuses_detail_within_run_without_merging_source_records(monkeypatch):
    calls = []
    monkeypatch.setattr(daily, "_screen_job", lambda *_: SimpleNamespace(analysis_status="jd_incomplete"))
    pipeline = daily.DailyRecruitmentPipeline(
        max_concurrency=4, jd_hydrator=lambda job: calls.append(job["id"]) or _reply(),
    )

    def works():
        return [SimpleNamespace(
            company=SimpleNamespace(id=f"co-{i}", crawler_config=lambda i=i: {
                "id": f"co-{i}", "name": "Official Portal", "crawler": "feishu", "careers_url": URL,
            }),
            accepted_jobs=[_job(i)], rejection_reasons=Counter(), jd_results=[],
        ) for i in range(9)]

    first = works()
    pipeline._prepare_job_details(first, {})
    assert len(calls) == 1
    assert len({work.accepted_jobs[0]["company_id"] for work in first}) == 9
    assert all(assess_jd_capture(work.accepted_jobs[0]).complete for work in first)
    assert all(len(work.jd_results) == 1 for work in first)
    assert sum(bool(work.jd_results[0].get("detail_reuse", {}).get("reused")) for work in first) == 8

    pipeline._prepare_job_details(works(), {})
    assert len(calls) == 2, "The cache must not silently persist across scheduled runs"


def test_eval_reuse_preserves_checkpoints_and_counts_real_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "ROOT", tmp_path)
    source = tmp_path / ".data" / "evals" / "input" / "checkpoints"
    output = tmp_path / ".data" / "evals" / "output"
    source.mkdir(parents=True)
    for i in range(9):
        (source / f"{i}.json").write_text(json.dumps({
            "company": f"Source {i}", "crawl_url": URL,
            "crawl": {"raw_jobs": [_job(i)]},
        }), encoding="utf-8")
    calls = []

    def fetch(job, *, timeout_seconds):
        assert timeout_seconds <= 5
        calls.append(job["company"])
        return _reply()

    summary = capture.run(source, output, workers=4, timeout=5, fetch=fetch)
    assert len(calls) == 1
    assert summary["attempted_job_count"] == 1
    assert summary["hydrated"] == 9
    assert summary["stage_complete"]
    rows = [json.loads(line) for line in (output / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len({row["company"] for row in rows}) == 9
    assert sum(row["request_made"] for row in rows) == 1
    assert all(assess_jd_capture(row["job"]).complete for row in rows)
    assert all(row["original_jd_raw"] == "unverified original" for row in rows)
    resumed = capture.run(source, output, workers=4, timeout=5, resume=True, fetch=fetch)
    assert len(calls) == 1
    assert resumed["attempted_job_count"] == 1
