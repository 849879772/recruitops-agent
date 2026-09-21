"""Synthetic, offline onboarding contracts; not live acceptance evidence."""
import json
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select

from packages.config import (DESKTOP_CAPABILITY_FIELDS, OFFERBIU_INDUSTRY_GROUP_OPTIONS,
                             anonymous_profile_payload, get_settings)
from packages.candidate_profile import load_candidate_profile
from packages.candidate_profile.context import build_scoring_context
from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery.offerbiu_refresh import OfferBiuRefreshService, capture_offerbiu_snapshot
from packages.matching.client import DeepSeekClient, DeepSeekClientError
from packages.matching.rules import profile_fingerprint
from packages.matching.service import MatchingService
from packages.matching.title_policy import screen_title_job
from packages.pipeline import run_daily_pipeline
from packages.scheduler import TaskContext, TaskType, build_runtime_task_handlers
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage
from tests.test_owner_configuration import owner
from tests.test_title_first_pipeline import _company, _config, _crawl, _detail, _job


class Session:
    def __init__(self, groups, returned_group=None):
        self.groups = groups
        self.returned_group = returned_group

    def get(self, url, *, params, **kwargs):
        assert [value for key, value in params if key == "industryGroup"] == sorted(self.groups)
        rows = [{"id": str(index), "companyName": f"Synthetic {group}",
                 "targetYears": [2027], "recruitType": "秋招",
                 "industryGroupCodes": [self.returned_group or group],
                 "applyUrl": f"https://company{index}.example.test/campus"}
                for index, group in enumerate(self.groups)]
        payload = {"success": True, "data": {"page": 0, "totalPages": 1,
                   "totalItems": len(rows), "previewLimited": False, "items": rows}}
        return type("Response", (), {"status_code": 200, "json": lambda self: payload})()


@pytest.mark.parametrize("group", [code for code, _ in OFFERBIU_INDUSTRY_GROUP_OPTIONS])
def test_every_configured_industry_reaches_capture_and_registration(group):
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    registry.upsert_source(source="offerbiu", source_record_id="old",
                           company_name="Unselected history", source_url="https://example.test",
                           entry_url="https://old.example.test/campus")
    service = OfferBiuRefreshService(registry, session=Session([group]),
                                    scope={"industry_groups": [group]})
    result = service.refresh(delay_seconds=0)
    assert result["complete"] and result["registered_entries"] == 1
    assert service.scope["industry_groups"] == [group]
    assert [row["company_name"] for row in result["pending_entries"]] == [f"Synthetic {group}"]
    assert registry.list_sources()["total"] == 2  # Preserve history, don't crawl it.


def test_all_industries_and_mismatched_response_fail_closed():
    groups = [code for code, _ in OFFERBIU_INDUSTRY_GROUP_OPTIONS]
    result = capture_offerbiu_snapshot(session=Session(groups), scope={"industry_groups": groups})
    assert result["complete"] and len(result["items"]) == len(groups)
    result = capture_offerbiu_snapshot(session=Session(["finance"], "internet-tech"),
                                      scope={"industry_groups": ["finance"]})
    assert not result["complete"] and result["stop_reason"] == "source_filter_mismatch"
    for invalid in ([], ["unknown"], "finance"):
        with pytest.raises(ValueError):
            capture_offerbiu_snapshot(session=Session([]), scope={"industry_groups": invalid})


def test_blank_profile_and_edited_keywords_are_not_developer_defaults():
    profile = anonymous_profile_payload()["profile"]
    assert not screen_title_job({"title": "C++ Engineer"}, profile).eligible
    profile["matching"]["title_keywords"] = ["Supply Chain"]
    assert screen_title_job({"title": "Supply Chain Analyst"}, profile).eligible
    before = profile_fingerprint(profile)
    profile["matching"]["title_keywords"] = ["Accounting"]
    assert profile_fingerprint(profile) != before
    assert not screen_title_job({"title": "Supply Chain Analyst"}, profile).eligible


@pytest.mark.parametrize("scheduled", [False, True])
def test_saved_provider_profile_reach_real_scoring_code_without_network(owner, monkeypatch, scheduled):
    client, headers, root, storage = owner
    connection = {"id": "fixture", "name": "Fixture", "provider": "openai-compatible",
                  "api_style": "openai", "base_url": "https://model.example.test/v1",
                  "model": "fixture-score", "api_key": "synthetic-not-a-credential"}
    profile = {"skills": ["SQL"], "matching": {"title_keywords": ["Supply Chain"],
               "project_evidence": ["Supply Chain SQL analysis"]},
               "scope": {"industry_groups": ["finance"]}}
    response = client.post("/api/local-ui/configuration/save", headers=headers,
                           json={"profile": profile, "model_connections": [connection],
                                 "settings": {"llm_enabled": True, "job_analysis_enabled": True},
                                 "active_model_connection_id": "fixture"})
    assert response.status_code == 200, response.text
    settings = get_settings()
    assert settings.offerbiu_industry_groups == ["finance"]
    saved_profile = load_candidate_profile(settings.candidate_profile_config)
    context = build_scoring_context(saved_profile)
    assert context.title_keywords == ["Supply Chain"]
    assert not screen_title_job({"title": "C++ Engineer"}, context).eligible
    calls = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(payload)
        assert endpoint == "https://model.example.test/v1/chat/completions"
        assert "Authorization" in headers and "x-api-key" not in headers
        assert payload["model"] == "fixture-score"
        assert "Supply Chain" in payload["messages"][1]["content"]
        result = {"matched_directions": [], "primary_match_direction": None,
                  "score_breakdown": {"core_direction": 25, "required_skills": 20,
                                      "project_evidence": 20, "engineering_stack": 10},
                  "evidence_level": "partial", "evidence": [], "summary": "Synthetic score"}
        return {"choices": [{"message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8}}

    wire = DeepSeekClient(api_key=settings.llm_api_key, model=settings.llm_model,
                          endpoint=settings.llm_endpoint, api_style=settings.model_api_style,
                          transport=transport)
    config = _config(root / "companies.yaml", _company("synthetic"))
    rows = [_job("good", "Supply Chain Analyst"),
            _job("unrelated", "C++ Engineer"),
            _job("intern-title", "Supply Chain Intern"),
            _job("intern-jd", "Supply Chain Operations"),
            _job("intern-type", "Supply Chain Planning")]
    rows[-1]["job_type"] = "internship"
    hydrated = []

    def hydrate(job):
        hydrated.append(job["id"])
        result = _detail(job)
        if job["id"] == "intern-jd":
            result["detail"] = "This internship position requires a six month internship."
            result["capture_evidence"]["content_sha256"] = sha256(result["detail"].encode()).hexdigest()
        return result

    run_daily_pipeline(companies_path=config, storage=storage,
                       profile=saved_profile, crawler=lambda company: _crawl(*rows),
                       jd_hydrator=hydrate, matcher=None if scheduled else MatchingService(wire))
    if scheduled:
        monkeypatch.setattr("packages.matching.client._default_transport", transport)
        handlers = build_runtime_task_handlers(settings=settings)
        result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](TaskContext(
            task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value, task_label="fixture",
            scheduled_for=datetime(2026, 9, 18, tzinfo=timezone.utc),
            run_id="synthetic-score-only", attempt=1, write_enabled=True,
            metadata={"details": {"mode": "score_only"}},
        ))
        assert result["status"] == "completed", result
    with storage.session() as db:
        jobs = list(db.scalars(select(JobSnapshot)))
        analyses = list(db.scalars(select(JobAnalysisSnapshot)))
    assert [row.title for row in jobs] == ["Supply Chain Analyst"]
    assert set(hydrated) == {"good", "intern-jd"}
    assert len(calls) == 1
    assert len(analyses) == 1 and analyses[0].analysis_status == "complete"
    assert analyses[0].model == "fixture-score"


def test_openai_truncation_is_failure_not_success():
    wire = DeepSeekClient(api_key="fixture", model="fixture", api_style="openai",
                          transport=lambda *args: {"choices": [{"finish_reason": "length",
                                                                 "message": {"content": "{}"}}]})
    with pytest.raises(DeepSeekClientError, match="response_truncated"):
        wire.complete(system_prompt="JSON", user_prompt="fixture")


def test_runtime_capabilities_can_be_saved_without_saving_launch_authority(owner):
    client, headers, root, _ = owner
    flags = {"automation_enabled": True, "vision_enabled": True,
             "mail_sync_on_startup": True}
    response = client.post("/api/local-ui/configuration/save", headers=headers,
                           json={"settings": flags})
    assert response.status_code == 200, response.text
    persisted = json.loads((root / ".data/settings/preferences.json").read_text(encoding="utf-8"))
    assert persisted["automation_enabled"] is True
    assert persisted["vision_enabled"] is True
    assert persisted["mail_sync_on_startup"] is False
    assert not (root / "config/runtime-capabilities.json").exists()
    for key in ("write_enabled", "api_token", "database_url", "agent_root"):
        response = client.post("/api/local-ui/configuration/save", headers=headers,
                               json={"settings": {key: "unauthorized"}})
        assert response.status_code == 422


@pytest.mark.parametrize("mask", [None, "invalid", "[]", "null", "{}",
                                  '{"llm_enabled":false}', '{"llm_enabled":"true"}'])
def test_desktop_mask_is_authoritative_after_preferences(owner, monkeypatch, mask):
    _, _, root, _ = owner
    directory = root / ".data/settings"
    directory.mkdir(parents=True, exist_ok=True)
    preferences = dict.fromkeys(DESKTOP_CAPABILITY_FIELDS, True)
    preferences.update({
        "llm_api_key": "synthetic-model-key",
        "model_api_style": "openai",
        "model_api_base_url": "https://model.example.test/v1",
        "model_name": "fixture-model",
        "mail_imap_host": "imap.example.test",
        "mail_imap_port": 993,
        "mail_imap_username": "fixture@example.test",
        "mail_imap_password": "synthetic-mail-password",
    })
    (directory / "preferences.json").write_text(json.dumps(preferences), encoding="utf-8")
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    if mask is None:
        monkeypatch.delenv("RECRUITOPS_DESKTOP_CAPABILITIES", raising=False)
    else:
        monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", mask)
    get_settings.cache_clear()
    assert all(getattr(get_settings(), key) is False for key in DESKTOP_CAPABILITY_FIELDS)
    monkeypatch.setenv("RECRUITOPS_ENV", "test")
    get_settings.cache_clear()
    assert all(getattr(get_settings(), key) is True for key in DESKTOP_CAPABILITY_FIELDS)


def test_explicit_completion_does_not_hot_activate_or_auto_complete(owner, monkeypatch):
    client, headers, root, _ = owner
    instance = "a" * 32
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", instance)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", "{}")
    get_settings.cache_clear()
    url = "/api/local-ui/configuration"
    marker = root / "config/runtime-capabilities.json"
    helper_calls = []

    def validate(settings, profile):
        helper_calls.append("validate")

    def complete(settings, profile):
        helper_calls.append("complete")
        # This fixture checks API delegation only; local_ui owns marker security.
        marker.write_text(json.dumps({"schema": 1, "instance_id": instance,
                                      "first_run_complete": True}), encoding="utf-8")

    monkeypatch.setattr("apps.api.local_ui.validate_desktop_onboarding", validate, raising=False)
    monkeypatch.setattr("apps.api.local_ui.complete_desktop_onboarding", complete, raising=False)
    assert client.post(url + "/read", headers=headers).status_code == 200
    assert not marker.exists()
    body = {"settings": {"llm_enabled": True, "job_analysis_enabled": True,
                         "mail_enabled": False, "codex_runtime_enabled": False}}
    assert client.post(url + "/save", headers=headers, json=body).status_code == 200
    assert not marker.exists() and not get_settings().llm_enabled
    assert helper_calls == []
    assert client.post(url + "/save", headers=headers,
                       json={**body, "complete_onboarding": True}).status_code == 200
    assert json.loads(marker.read_text()) == {
        "schema": 1, "instance_id": instance, "first_run_complete": True}
    assert helper_calls == ["validate", "complete"]
    read = client.post(url + "/read", headers=headers).json()
    assert read["configured_capabilities"]["llm_enabled"] is False
    assert read["settings"]["llm_enabled"] is False and read["restart_required"]
    monkeypatch.setenv("RECRUITOPS_DESKTOP_CAPABILITIES", '{"llm_enabled":true}')
    get_settings.cache_clear()  # Simulates new owned child environment, no actual restart.
    assert get_settings().llm_enabled and not get_settings().job_analysis_enabled


def test_completion_rejected_before_any_configuration_write(owner, monkeypatch):
    from fastapi import HTTPException

    client, headers, root, _ = owner
    monkeypatch.setenv("RECRUITOPS_ENV", "desktop-isolated")
    def reject(*args):
        raise HTTPException(403, "fixture ownership rejection")
    monkeypatch.setattr("apps.api.local_ui.validate_desktop_onboarding", reject, raising=False)
    response = client.post("/api/local-ui/configuration/save", headers=headers,
                           json={"settings": {"llm_enabled": True}, "complete_onboarding": True})
    assert response.status_code == 403
    assert not (root / ".data/settings/preferences.json").exists()
    assert not (root / "config/runtime-capabilities.json").exists()


def test_keyless_draft_saves_and_credentials_do_not_enable_features(owner):
    client, headers, _, _ = owner
    connection = {"id": "draft", "name": "Draft", "provider": "openai-compatible",
                  "api_style": "openai", "base_url": "https://model.example.test/v1",
                  "model": "fixture", "api_key": ""}
    payload = {"model_connections": [connection], "active_model_connection_id": "draft",
               "profile": {"skills": [], "matching": {"title_keywords": ["测试工程师"]}},
               "settings": {"mail_enabled": False}}
    response = client.post("/api/local-ui/configuration/save", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert not get_settings().llm_api_key  # Never reuse another provider's key.
    connection["api_key"] = "fixture"
    payload["settings"].update(llm_enabled=False, job_analysis_enabled=False,
                                codex_runtime_enabled=False,
                                mail_imap_username="synthetic@example.test",
                                mail_imap_password="fixture")
    response = client.post("/api/local-ui/configuration/save", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    settings = get_settings()
    assert not any((settings.llm_enabled, settings.job_analysis_enabled,
                    settings.codex_runtime_enabled, settings.mail_enabled))
