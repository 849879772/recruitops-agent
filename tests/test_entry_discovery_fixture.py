from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals" / "fixtures" / "entry_discovery_36_20260907.json"
COMPANIES = ROOT / "config" / "companies.yaml"
DISCOVERY_NOTE = "recruitment_entry_discovery_required"
CONFIG_FIELDS = (
    "id",
    "name",
    "industries",
    "careers_url",
    "crawler",
    "integration_status",
    "integration_note",
    "recruitment_unit_id",
    "recruitment_unit_name",
    "organization_id",
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _load_expected_companies() -> list[dict]:
    configured = yaml.safe_load(COMPANIES.read_text(encoding="utf-8"))["companies"]
    return [
        company
        for company in configured
        if DISCOVERY_NOTE in str(company.get("integration_note", ""))
    ]


def test_entry_discovery_fixture_has_the_frozen_queue_shape() -> None:
    fixture = _load_fixture()
    rows = fixture["companies"]

    assert fixture["fixture_version"] == "entry-discovery-36-v1"
    assert fixture["source_config_path"] == "config/companies.yaml"
    assert len(rows) == 36
    assert len({row["id"] for row in rows}) == 36

    debug = rows[:6]
    acceptance = rows[6:]
    debug_ids = {row["id"] for row in debug}
    acceptance_ids = {row["id"] for row in acceptance}
    assert len(debug) == 6
    assert len(acceptance) == 30
    assert {row["cohort"] for row in debug} == {"debug"}
    assert {row["cohort"] for row in acceptance} == {"acceptance"}
    assert debug_ids.isdisjoint(acceptance_ids)


def test_entry_discovery_fixture_matches_current_config_in_order() -> None:
    fixture = _load_fixture()
    rows = fixture["companies"]
    expected = _load_expected_companies()

    assert len(expected) == 36
    assert [row["id"] for row in rows] == [company["id"] for company in expected]

    for row, company in zip(rows, expected):
        for field in CONFIG_FIELDS:
            assert row[field] == company.get(field), (
                f"{row['id']} field {field!r} differs from config"
            )
        assert DISCOVERY_NOTE in str(company.get("integration_note", ""))
