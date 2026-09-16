from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals" / "fixtures" / "daily_scope_10_20260906.json"
COMPANIES = ROOT / "config" / "companies.yaml"


def test_daily_scope_fixture_has_ten_unique_connected_companies() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    configured = yaml.safe_load(COMPANIES.read_text(encoding="utf-8"))["companies"]
    by_id = {row["id"]: row for row in configured}
    rows = fixture["companies"]

    assert fixture["fixture_version"] == "daily-scope-10-v1"
    assert len(rows) == 10
    assert len({row["id"] for row in rows}) == 10
    assert {row["crawler"] for row in rows} == {
        "alibaba",
        "baidu",
        "beisen",
        "bilibili",
        "feishu",
        "hotjob",
        "lenovo",
        "moka",
        "job4399",
        "tencent",
    }
    for row in rows:
        actual = by_id[row["id"]]
        assert actual["integration_status"] == "connected"
        assert actual["name"] == row["name"]
        assert actual["crawler"] == row["crawler"]
        assert actual["careers_url"] == row["careers_url"]
