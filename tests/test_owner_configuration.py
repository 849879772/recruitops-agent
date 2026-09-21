import base64
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import configuration, local_ui, main
from packages.config import Settings, get_settings
from packages.storage import Storage, ApplicationSnapshot


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUITOPS_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("RECRUITOPS_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RECRUITOPS_WRITE_ENABLED", "true")
    monkeypatch.setenv("RECRUITOPS_LLM_API_KEY", "test-secret")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/candidate_profile.yaml").write_text('profile: {skills: [Python]}', encoding="utf-8")
    get_settings.cache_clear()
    storage = Storage.from_url(get_settings().database_url, initialize=True)
    client = TestClient(main.app, base_url="http://127.0.0.1:8765")
    headers = {"Origin": "http://127.0.0.1:8765", "X-RecruitOps-Local-UI": "1"}
    yield client, headers, tmp_path, storage
    get_settings.cache_clear()


def test_secret_redaction_roundtrip_and_same_origin(owner):
    client, headers, root, _ = owner
    url = "/api/local-ui/configuration"
    assert client.post(url + "/read").status_code == 403
    assert client.post(url + "/read", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    read = client.post(url + "/read", headers=headers)
    assert "test-secret" not in read.text
    payload = read.json()
    payload["profile"]["matching"]["title_keywords"] = ["数据分析"]
    result = client.post(url + "/save", headers=headers, json={"settings": {"llm_api_key": "new-secret"}, "profile": payload["profile"]})
    assert result.status_code == 200, result.text
    assert "new-secret" not in result.text
    assert get_settings().llm_api_key == "new-secret"
    assert get_settings().candidate_profile_config.parent == root / ".data/settings"
    client.post(url + "/save", headers=headers, json={"settings": {"llm_api_key": ""}})
    assert get_settings().llm_api_key == "new-secret"
    assert client.post(url + "/save", headers=headers, json={"settings": {"database_url": "evil"}}).status_code == 422
    assert client.post(url + "/save", headers=headers, json={"settings": {"mail_imap_port": "oops"}}).status_code == 422


def upload(name, content):
    return {"filename": name, "content_base64": base64.b64encode(content.encode()).decode()}


@pytest.mark.parametrize("keywords", [[], ["", " \t", "\n"], None])
def test_profile_save_rejects_empty_keywords_without_writes(owner, keywords):
    client, headers, root, _ = owner
    matching = {} if keywords is None else {"title_keywords": keywords}
    response = client.post("/api/local-ui/configuration/save", headers=headers,
        json={"settings": {"llm_api_key": "must-not-save"},
              "profile": {"skills": ["Python"], "matching": matching}})
    assert response.status_code == 422
    assert "岗位筛选关键词不能为空" in response.json()["detail"]
    assert not (root / ".data/settings/preferences.json").exists()
    assert not (root / ".data/settings/candidate_profile.yaml").exists()


def test_profile_save_trims_keywords(owner):
    client, headers, root, _ = owner
    response = client.post("/api/local-ui/configuration/save", headers=headers,
        json={"profile": {"skills": ["Python"], "matching": {"title_keywords": [" ", " Python "]}}})
    assert response.status_code == 200
    saved = json.loads((root / ".data/settings/candidate_profile.yaml").read_text(encoding="utf-8"))
    assert saved["profile"]["matching"]["title_keywords"] == ["Python"]


def test_models_share_one_base_and_legacy_preferences_are_normalized(owner):
    client, headers, root, _ = owner
    directory = root / ".data/settings"
    directory.mkdir(parents=True)
    path = directory / "preferences.json"
    path.write_text(json.dumps({"llm_model": "old-score", "codex_model": "old-agent",
        "vision_model": "old-vision", "codex_model_base_url": "https://legacy.example/v1",
        "llm_api_key": "keep-secret"}), encoding="utf-8")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_model == settings.codex_model == settings.vision_model == "deepseek-flash"
    assert settings.model_api_base_url == "https://legacy.example"
    response = client.post("/api/local-ui/configuration/read", headers=headers).json()
    assert "llm_model" not in response["settings"] and "vision_endpoint" not in response["settings"]
    result = client.post("/api/local-ui/configuration/save", headers=headers,
        json={"settings": {"model_api_base_url": "https://proxy.example/api/v1/", "llm_api_key": ""}})
    assert result.status_code == 200, result.text
    settings = get_settings()
    assert settings.codex_model_base_url == "https://proxy.example/api"
    assert settings.llm_endpoint == "https://proxy.example/api/anthropic/v1/messages"
    assert settings.vision_endpoint == "https://proxy.example/api/chat/completions"
    assert settings.llm_api_key == "keep-secret"
    assert "llm_model" not in json.loads(path.read_text(encoding="utf-8"))
    assert client.post("/api/local-ui/configuration/save", headers=headers,
        json={"settings": {"llm_model": "another-model"}}).status_code == 422


def test_environment_cannot_split_models(monkeypatch):
    monkeypatch.setenv("RECRUITOPS_LLM_MODEL", "old-score")
    monkeypatch.setenv("RECRUITOPS_VISION_MODEL", "old-vision")
    settings = Settings(model_api_base_url="https://api.deepseek.com")
    assert settings.llm_model == settings.vision_model == settings.codex_model == "deepseek-flash"


def test_configuration_exposes_all_offerbiu_industry_groups(owner):
    client, headers, _, _ = owner
    payload = client.post("/api/local-ui/configuration/read", headers=headers).json()
    options = payload["options"]["industry_groups"]
    assert len(options) == 14
    assert {item["code"] for item in options} >= {
        "internet-tech", "semiconductor-hardware", "finance",
        "public-research-nonprofit", "logistics-supply-chain", "other",
    }
    assert sum(bool(item["default"]) for item in options) == 3
    assert payload["profile"]["scope"]["industry_groups"] == [
        "internet-tech", "manufacturing-equipment", "auto-transport-equipment",
    ]
    saved = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "settings": {"offerbiu_industry_groups": ["finance"]},
    })
    assert saved.status_code == 200, saved.text
    assert get_settings().offerbiu_industry_groups == ["finance"]
    assert client.post("/api/local-ui/configuration/read", headers=headers).json()["profile"]["scope"]["industry_groups"] == ["finance"]


def test_multiple_model_connections_are_redacted_and_active_one_becomes_effective(owner):
    client, headers, root, _ = owner
    connections = [
        {"id": "deepseek-main", "name": "DeepSeek", "provider": "deepseek",
         "api_style": "anthropic", "base_url": "https://api.deepseek.com",
         "model": "deepseek-flash", "api_key": "deep-secret"},
        {"id": "backup", "name": "备用", "provider": "openai-compatible",
         "api_style": "openai", "base_url": "https://models.example/v1",
         "model": "example-model", "api_key": "backup-secret"},
    ]
    response = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "settings": {}, "model_connections": connections,
        "active_model_connection_id": "backup",
    })
    assert response.status_code == 200, response.text
    read = client.post("/api/local-ui/configuration/read", headers=headers)
    assert "deep-secret" not in read.text and "backup-secret" not in read.text
    assert read.json()["active_model_connection_id"] == "backup"
    assert all(item["key_configured"] for item in read.json()["model_connections"])
    settings = get_settings()
    assert settings.model_api_style == "openai"
    assert settings.model_name == "example-model"
    assert settings.llm_endpoint == "https://models.example/v1/chat/completions"
    stored = json.loads((root / ".data/settings/model_connections.json").read_text(encoding="utf-8"))
    assert stored["active_id"] == "backup"


def test_source_shaped_profile_is_normalized_without_persisting_unmigrated_fields(owner):
    client, headers, root, _ = owner
    profile = {
        "degree": "硕士",
        "job_type": "校招",
        "direction": "供应链分析",
        "skills": ["SQL"],
        "matching": {
            "title_keywords": ["供应链分析"],
            "directions": [{"name": "供应链分析", "keywords": ["供应链分析"], "exclude_keywords": []}],
        },
        "scope": {"industry_groups": ["finance"]},
        "exclusions": {"internships": "exclude", "social": True},
    }
    response = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "settings": {}, "profile": profile,
    })

    assert response.status_code == 200, response.text
    stored = json.loads((root / ".data/settings/candidate_profile.yaml").read_text(encoding="utf-8"))
    assert "scope" not in stored["profile"]
    assert "exclusions" not in stored["profile"]
    assert "directions" not in stored["profile"]["matching"]
    read = client.post("/api/local-ui/configuration/read", headers=headers).json()
    assert read["settings"]["offerbiu_industry_groups"] == ["finance"]
    assert read["profile"]["matching"]["primary_directions"] == ["供应链分析"]


def test_model_connection_button_contract_checks_openai_chat_and_assistant(owner, monkeypatch):
    client, headers, _, _ = owner
    calls = []

    def fake_transport(endpoint, request_headers, payload, timeout):
        calls.append((endpoint, request_headers, payload, timeout))
        if endpoint.endswith("/chat/completions"):
            return {"model": "example-model", "choices": [{"message": {"content": '{"status":"ok"}'}}]}
        return {"id": "resp_test"}

    monkeypatch.setattr("packages.matching.client._default_transport", fake_transport)
    response = client.post("/api/local-ui/configuration/model/test", headers=headers, json={
        "id": "backup",
        "provider": "openai-compatible",
        "api_style": "openai",
        "base_url": "https://models.example/v1",
        "model": "example-model",
        "api_key": "test-key",
    })

    assert response.status_code == 200, response.text
    assert response.json()["structured_output"] is True
    assert response.json()["assistant_runtime"] is True
    assert [call[0] for call in calls] == [
        "https://models.example/v1/chat/completions",
        "https://models.example/v1/responses",
    ]


def test_model_provider_and_wire_style_cannot_be_mismatched(owner):
    client, headers, _, _ = owner
    connection = {
        "id": "bad", "name": "错误连接", "provider": "deepseek",
        "api_style": "openai", "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash", "api_key": "test-key",
    }

    saved = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "settings": {}, "model_connections": [connection],
        "active_model_connection_id": "bad",
    })
    tested = client.post("/api/local-ui/configuration/model/test", headers=headers, json={
        key: value for key, value in connection.items() if key != "name"
    })

    assert saved.status_code == tested.status_code == 422


def test_resume_parse_reports_missing_model_before_any_transport(owner):
    client, headers, _, _ = owner
    response = client.post(
        "/api/local-ui/configuration/resume/parse",
        headers=headers,
        json={"text": "匿名候选人参与供应链分析项目，使用 SQL 完成库存预测和报表开发。"},
    )
    assert response.status_code == 422
    assert "请先保存 API 密钥并启用模型调用" in response.text


def test_resume_parse_is_anonymous_profile_driven_and_draft_only(owner, monkeypatch):
    client, headers, root, _ = owner
    model = {
        "id": "local-openai", "name": "离线夹具模型", "provider": "openai-compatible",
        "api_style": "openai", "base_url": "https://models.example/v1",
        "model": "fixture-model", "api_key": "fixture-key",
    }
    saved = client.post("/api/local-ui/configuration/save", headers=headers, json={
        "settings": {"llm_enabled": True}, "model_connections": [model],
        "active_model_connection_id": "local-openai",
    })
    assert saved.status_code == 200, saved.text
    resume_text = "硕士。参与供应链分析项目，使用 SQL 完成库存预测和报表开发。"
    draft = {
        "degree": {"value": "硕士", "evidence": "硕士"},
        "skills": [{"value": "SQL 数据分析", "evidence": "使用 SQL 完成库存预测和报表开发"}],
        "supporting_skills": [],
        "projects": ["使用 SQL 进行供应链预测与报表分析"],
        "title_keywords": [{"value": "数据分析工程师", "evidence": "参与供应链分析项目，使用 SQL"}],
        "directions": [{"name": "供应链分析", "keywords": ["数据分析工程师"], "evidence": "参与供应链分析项目，\n使用 SQL"}],
    }

    def fake_transport(endpoint, _headers, _payload, _timeout):
        assert endpoint == "https://models.example/v1/chat/completions"
        return {"model": "fixture-model", "choices": [{"message": {"content": json.dumps(draft, ensure_ascii=False)}}]}

    monkeypatch.setattr("packages.matching.client._default_transport", fake_transport)
    response = client.post("/api/local-ui/configuration/resume/parse", headers=headers, json={"text": resume_text})

    assert response.status_code == 200, response.text
    assert response.json()["draft"] == draft
    assert not (root / ".data/settings/candidate_profile.yaml").exists()

    for keywords in ([item["value"] for item in draft["title_keywords"]], ["商业分析", "数据产品"]):
        saved = client.post("/api/local-ui/configuration/save", headers=headers, json={
            "profile": {"skills": ["SQL 数据分析"], "matching": {
                "title_keywords": keywords, "project_evidence": [resume_text],
            }},
        })
        assert saved.status_code == 200, saved.text
        persisted = json.loads((root / ".data/settings/candidate_profile.yaml").read_text(encoding="utf-8"))
        assert persisted["profile"]["matching"]["title_keywords"] == keywords


def test_mail_connection_test_uses_read_only_fixture(owner, monkeypatch):
    client, headers, _, _ = owner
    calls = []

    class Result:
        messages = ["one"]

    class Connector:
        def __init__(self, config):
            calls.append(config)

        def fetch_since(self, *, limit):
            assert limit == 1
            return Result()

    monkeypatch.setattr("packages.recruitment_mail.connectors.ImapReadOnlyConnector", Connector)
    response = client.post("/api/local-ui/configuration/mail/test", headers=headers, json={
        "host": "imap.example.test",
        "port": 993,
        "username": "anonymous@example.test",
        "password": "fixture-password",
        "mailbox": "INBOX",
    })

    assert response.status_code == 200, response.text
    assert response.json()["read_only"] is True
    assert response.json()["sample_count"] == 1
    assert calls[0].host == "imap.example.test"


def test_configuration_api_exposes_managed_connections_and_onboarding_contract():
    paths = {route.path for route in configuration.router.routes}
    assert "/api/local-ui/configuration/read" in paths
    assert "/api/local-ui/configuration/save" in paths
    assert "/api/local-ui/configuration/model/test" in paths
    assert "/api/local-ui/configuration/mail/test" in paths
    assert "/api/local-ui/configuration/resume/parse" in paths


def test_fresh_configuration_bootstraps_anonymous_files_and_precise_readiness(owner):
    client, headers, root, _ = owner
    (root / "config/candidate_profile.yaml").unlink()
    (root / "config/companies.yaml").unlink(missing_ok=True)
    (root / "config/candidate_profile.example.yaml").write_text(
        "profile:\n  direction: robotics/C++/AI\n", encoding="utf-8"
    )
    (root / "config/companies.example.yaml").write_text(
        "companies:\n  - name: Example Robotics\n", encoding="utf-8"
    )
    response = client.post("/api/local-ui/configuration/read", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["bootstrap"] == {"companies_created": True, "profile_created": True}
    assert payload["profile"]["skills"] == []
    assert payload["profile"]["matching"]["title_keywords"] == []
    assert yaml.safe_load((root / "config/companies.yaml").read_text(encoding="utf-8")) == {"companies": []}
    assert "robotics" not in (root / "config/candidate_profile.yaml").read_text(encoding="utf-8")
    assert "Example Robotics" not in (root / "config/companies.yaml").read_text(encoding="utf-8")
    assert payload["onboarding"]["ready"] is False
    assert payload["onboarding"]["missing"] == ["model", "resume", "title_keywords"]


def test_resume_preview_not_applied_until_save(owner):
    client, headers, root, _ = owner
    result = client.post("/api/local-ui/configuration/resume", headers=headers,
        json=upload("resume.txt", "软件开发项目，使用 Python 开发系统、测试和数据库，负责数据分析。" * 2))
    assert result.status_code == 200
    assert "软件开发" in result.json()["text"]
    assert not (root / ".data/settings/candidate_profile.yaml").exists()
    assert client.post("/api/local-ui/configuration/resume", headers=headers,
        json=upload("bad.pdf", "not PDF")).status_code == 422


def test_import_is_idempotent_preserves_stage_and_is_atomic(owner):
    client, headers, _, storage = owner
    path = "/api/local-ui/configuration/applications/import"
    body = upload("applications.csv", "公司,岗位,阶段,投递进度网址\n测试公司,工程师,笔试,https://example.com/applications\n")
    response = client.post(path, headers=headers, json=body)
    assert response.json() == {"inserted": 1, "skipped": 0}, response.text
    assert client.post(path, headers=headers, json=body).json() == {"inserted": 0, "skipped": 1}
    with storage.session() as session:
        assert session.scalar(select(ApplicationSnapshot)).stage == "written"
    bad = upload("applications.json", json.dumps([
        {"company_name": "new", "job_title": "job"},
        {"company_name": "bad", "job_title": "job", "record_url": "javascript:alert(1)"}]))
    assert client.post(path, headers=headers, json=bad).status_code == 422
    with storage.session() as session:
        assert len(list(session.scalars(select(ApplicationSnapshot)))) == 1


def test_custom_title_keywords_include_and_exclude():
    from packages.matching.title_policy import screen_title_job
    profile = {"matching": {"title_keywords": ["数据分析"], "excluded_title_keywords": ["销售"]}}
    assert screen_title_job({"title": "数据分析师"}, profile).eligible
    for title in ["数据分析实习生", "数据分析博士", "销售数据分析师", "机器人软件工程师"]:
        assert not screen_title_job({"title": title}, profile).eligible


def test_user_title_keywords_override_developer_direction_defaults():
    from packages.matching.title_policy import screen_title_job

    profile = {
        "matching": {
            "title_keywords": ["供应链分析"],
            "primary_directions": ["供应链分析"],
        }
    }
    assert screen_title_job({"title": "供应链分析师"}, profile).eligible
    assert not screen_title_job({"title": "机器人软件工程师"}, profile).eligible
