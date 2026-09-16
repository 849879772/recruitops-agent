import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    client = TestClient(main.app, base_url="http://127.0.0.1:8012")
    headers = {"Origin": "http://127.0.0.1:8012", "X-RecruitOps-Local-UI": "1"}
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


def test_configuration_page_has_only_one_model_connection():
    html = (Path(__file__).resolve().parents[1] / "apps/web/index.html").read_text(encoding="utf-8")
    assert 'name="model_api_base_url"' in html
    for field in ("llm_model", "codex_model", "vision_model", "llm_endpoint", "vision_endpoint", "codex_model_base_url"):
        assert f'name="{field}"' not in html


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
