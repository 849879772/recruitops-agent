"""Full resolver/verifier regressions, with disposable SQLite and a fake provider."""
from copy import deepcopy

import pytest

from packages.storage.models import ApplicationSnapshot
from tests.test_application_page_model_fallback import URL, candidate, case


@pytest.mark.parametrize("label,status,saved,state,wrote,expected_calls", [
    ("初筛中", "applied", "applied", "unchanged", False, 0),
    ("HR筛选-进行中", "applied", "applied", "unchanged", False, 0),
    ("笔试中", "written", "applied", "updated", True, 1),
    ("笔试中", "written", "written", "unchanged", False, 1),
])
def test_visual_current_state_survives_history_through_final_write_guard(
        tmp_path, monkeypatch, label, status, saved, state, wrote, expected_calls):
    title = "智能工具开发工程师"
    text = f"{title}\n投递简历 2026-09-30\n{label}"
    dom = {"title": title, "context": f"{title}\n投递简历 2026-09-30",
           "label": "", "raw_status_labels": [], "signals": {"has_date": True}}
    reading = {"reading_version": "literal-cards-v1", "text": text, "confidence": .98,
               "model": "fixture", "image_sha256": "a" * 64,
               "cards": [{"title": title, "text": text, "current": True, "current_label": label}]}
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": saved}],
        observation={"application_records": [dom], "vision": reading},
        candidates=[candidate(title, label=label, ref=None, quote="", evidence_ref="vision:card:0",
                              observed_status=status, current_node_ref="vision:card:0")])
    original = deepcopy(dom)
    result = run(visual=True)["24"]
    assert result["state"] == state and result["wrote"] is wrote, result
    assert result["observed_status"] == status and result["observed_label"] == label
    assert dom == original and len(client.calls) == expected_calls
    with repository.storage.session() as session:
        app = session.get(ApplicationSnapshot, "24")
        assert app.stage == status
        assert len(app.stage_history) == (1 if wrote else 0)
    # Cached proposals still pass verification; the same evidence cannot add
    # duplicate stage history, even after an actual forward write in the test DB.
    again = run(visual=True)["24"]
    assert again["state"] == "unchanged" and not again["wrote"]
    assert len(client.calls) == expected_calls


@pytest.mark.parametrize("saved", ["written", "interview1", "offer"])
def test_dated_submission_alias_cannot_regress_a_later_saved_stage(tmp_path, monkeypatch, saved):
    title = "应用软件工程师(J12345)"
    text = f"{title}\n2026-09-30 22:31 投递"
    reading = {"reading_version": "literal-cards-v1", "text": text, "confidence": .98,
               "model": "fixture", "image_sha256": "a" * 64,
               "cards": [{"title": title, "text": text, "current": False, "current_label": ""}]}
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": saved}],
        observation={"application_records": [], "vision": reading},
        candidates=[candidate(title, label="2026-09-30 22:31 投递", ref=None, quote="",
                              evidence_ref="vision:card:0", observed_status="applied")])
    result = run(visual=True)["24"]
    assert not result.get("wrote") and result["state"] == "unchanged", result
    with repository.storage.session() as session:
        app = session.get(ApplicationSnapshot, "24")
        assert app.stage == saved and app.stage_history == []
