from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from scripts import benchmark_detail_reuse as module


def _detail_hash(value: str) -> str:
    return sha256(value.strip().encode()).hexdigest()


def _job(title: str, url: str, city: str = "北京") -> dict[str, object]:
    return {"title": title, "city": city, "cohort": 2027, "cohort_status": "confirmed", "jd_url": url, "jd_raw": "", "link_kind": "detail"}


def _fixture(item: dict[str, object], detail: str | None = None, *, matched: bool = True) -> dict[str, object]:
    detail = detail or f"Verified detail for {item['title']}"
    parsed = module.official_url(item["detail_url"])
    assert parsed is not None
    hydration: dict[str, object] = {"detail": detail, "status": "complete", "source": "fixture_official_api", "detail_url": item["detail_url"], "request_made": True, "identity_status": "matched" if matched else "request_bound", "identity_evidence": [f"native_id:{parsed[2]}", f"title:{item['title']}"], "capture_evidence": {"status": "complete", "method": "official_api", "source_url": item["detail_url"], "identity_verified": True, "terminal_observed": True, "remaining_controls": [], "content_sha256": _detail_hash(detail)}}
    return {"job_key": item["job_key"], "input_sha256": item["input_hash"], "hydration": hydration, "hydration_outcome": "hydrated"}


def _write_fixture(root: Path, item: dict[str, object], payload: dict[str, object]) -> None:
    directory = root / "hydration" / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{item['job_key']}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _input(tmp_path: Path) -> tuple[Path, Path, Path, list[dict[str, object]]]:
    raw = tmp_path / "raw-jobs.jsonl"
    checkpoints = tmp_path / "hydration" / "checkpoints"
    summary = tmp_path / "hydration" / "summary.json"
    records: list[dict[str, object]] = []
    bytedance = "https://jobs.bytedance.com/campus/position/7667551275686594821/detail"
    for source in ("ByteDance A", "ByteDance B", "ByteDance C"):
        records.append({"company_label": source, "crawl_url": "https://official.example/campus", "job": _job("AI Agent Engineer", bytedance)})
    feishu = "https://tenant.jobs.feishu.cn/398875/position/123/detail"
    for source in ("Feishu A", "Feishu B"):
        records.append({"company_label": source, "crawl_url": "https://official.example/campus", "job": _job("Feishu Engineer", feishu, "上海")})
    unknown = "https://jobs.bytedance.com/campus/position/7667551275686594999/detail"
    for source in ("Unverified A", "Unverified B"):
        records.append({"company_label": source, "crawl_url": "https://official.example/campus", "job": _job("Unverified Engineer", unknown)})
    raw.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    groups, _ = module.select_groups(raw, limit=3, rows_per_group=9)
    rows = [item for group in groups for item in group["rows"]]
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({"stage_complete": False, "status_counts": {"hydrated": 5}}), encoding="utf-8")
    return raw, checkpoints, summary, rows


class FakeReusingDetailHydrator:
    def __init__(self, fetch):
        self.fetch = fetch
        self.cache: dict[str, dict[str, object]] = {}

    def __call__(self, job, *, timeout_seconds):
        key = job["__benchmark_job_key"]
        url = module.normalize_url(job["jd_url"])
        if url in self.cache:
            return {**self.cache[url]["hydration"], "_detail_reuse": {"reused": True, "representative_job_key": self.cache[url]["job_key"]}}
        result = self.fetch(job, timeout_seconds=timeout_seconds)
        if result["status"] == "complete" and result["identity_status"] == "matched":
            self.cache[url] = {"job_key": key, "hydration": result}
        return {**result, "_detail_reuse": {"reused": False}}


def test_bounded_fixture_replay_counts_calls_and_keeps_source_labels(tmp_path):
    raw, checkpoints, summary, rows = _input(tmp_path)
    groups, _ = module.select_groups(raw, limit=3, rows_per_group=9)
    for item in rows:
        if str(item["url"]).endswith("4821/detail") and item["source_label"] == "ByteDance A":
            _write_fixture(tmp_path, item, _fixture(item))
        elif str(item["url"]).endswith("position/123/detail") and item["source_label"] == "Feishu A":
            _write_fixture(tmp_path, item, _fixture(item, "Shared Feishu detail"))
    unknown = next(item for item in rows if str(item["url"]).endswith("4999/detail"))
    unverified = _fixture(unknown, matched=False)
    unverified["hydration"]["identity_evidence"] = []
    _write_fixture(tmp_path, unknown, unverified)

    before = raw.read_bytes()
    report = module.run(raw, checkpoints, summary, groups=5, rows_per_group=9, hydrator_cls=FakeReusingDetailHydrator)

    assert raw.read_bytes() == before
    assert report["selection"]["selected_groups"] == 3
    assert report["verified_group_count"] == 2
    assert report["unverified_group_count"] == 1
    assert report["baseline"]["fetch_calls"] == 5
    assert report["reuse"]["status"] == "ok"
    assert report["reuse"]["fetch_calls"] == 2
    assert report["request_count_reduction"] == 3
    assert report["baseline"]["results"]["success"] == report["reuse"]["results"]["success"] == 5
    assert report["comparison_summary"] == {"rows": 5, "hash_equal": 5, "identity_consistent": 5, "source_preserved": 5, "matched": 5}
    assert any(row["fixture_class"] == "unverified" for group in report["groups"] if group["verification"] == "verified" for row in group["rows"])
    assert len(groups) == 3


@pytest.mark.parametrize("kind", ["network", "db"])
def test_network_or_db_path_fails_directly(tmp_path, kind):
    raw, checkpoints, summary, rows = _input(tmp_path)
    for item in rows[:2]:
        _write_fixture(tmp_path, item, _fixture(item))

    class ForbiddenHydrator:
        def __init__(self, fetch):
            del fetch
            if kind == "network":
                socket.create_connection(("example.invalid", 443))
            sqlite3.connect(":memory:")

    with pytest.raises(module.OfflineAccessError):
        module.run(raw, checkpoints, summary, groups=1, rows_per_group=2, hydrator_cls=ForbiddenHydrator)


def test_missing_component_is_blocked_and_fixture_replay_is_not_public_speedup(tmp_path, monkeypatch):
    raw, checkpoints, summary, rows = _input(tmp_path)
    for item in rows[:3]:
        _write_fixture(tmp_path, item, _fixture(item))
    monkeypatch.setattr(module, "load_hydrator", lambda: (_ for _ in ()).throw(ModuleNotFoundError("detail_reuse")))
    report = module.run(raw, checkpoints, summary, groups=1, rows_per_group=3)
    assert report["reuse"]["status"] == "blocked"
    assert "detail_reuse" in report["reuse"]["reason"]
    assert report["fixture_replay"]["is_public_speedup"] is False
    assert report["db_writes"] == report["model_calls"] == 0


def test_request_bound_native_identity_replays_with_real_hydrator(tmp_path):
    raw, checkpoints, summary, rows = _input(tmp_path)
    representative = rows[0]
    fixture = _fixture(representative, matched=False)
    _write_fixture(tmp_path, representative, fixture)
    report = module.run(raw, checkpoints, summary, groups=1, rows_per_group=3)
    assert report["verified_group_count"] == 1
    assert report["baseline"]["fetch_calls"] == 3
    assert report["reuse"]["fetch_calls"] == 1
    assert report["comparison_summary"]["matched"] == 3
    assert representative["job_key"] == module.capture.job_identity(
        representative["company"], representative["job"]
    )["job_key"]


def test_conflicting_request_bound_native_ids_are_not_qualified(tmp_path):
    _raw, _checkpoints, _summary, rows = _input(tmp_path)
    fixture = _fixture(rows[0], matched=False)["hydration"]
    fixture["identity_evidence"].append("native_id:999")
    assert module.evidence_ok(rows[0], fixture)[0] == "unverified"
