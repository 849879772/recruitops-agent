import json
from types import SimpleNamespace
from tests.test_owner_configuration import owner
from packages.config import get_settings
from packages.matching.client import DeepSeekClientError


def test_parse_is_grounded_preview_only(owner, monkeypatch):
    client, headers, root, _ = owner
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "true")
    get_settings.cache_clear()
    resume = "硕士学历；使用 Python 开发机器人系统，Linux 环境完成控制模块开发与调试。"
    payload = {"degree": {"value": "硕士", "evidence": "硕士学历"},
        "skills": [{"value": "Python", "evidence": "使用 Python 开发机器人系统"}],
        "supporting_skills": [], "projects": ["Linux 环境完成控制模块开发与调试。"]}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["thinking_enabled"] is False and kwargs["max_attempts"] == 1
        def complete_structured(self, **kwargs):
            assert kwargs["user_prompt"] == resume
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr("packages.matching.client.DeepSeekClient", Client)
    url = "/api/local-ui/configuration/resume/parse"
    assert client.post(url, json={"text": resume}).status_code == 403
    response = client.post(url, headers=headers, json={"text": resume})
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["skills"][0]["value"] == "Python"
    assert not (root / ".data/settings/candidate_profile.yaml").exists()
    payload["skills"][0]["value"] = "invented skill"
    edited = client.post(url, headers=headers, json={"text": resume})
    assert edited.status_code == 200
    assert edited.json()["draft"]["skills"][0]["value"] == "invented skill"
    assert not (root / ".data/settings/candidate_profile.yaml").exists()


def test_disabled_model_does_not_call_provider(owner, monkeypatch):
    client, headers, _, _ = owner
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "false")
    get_settings.cache_clear()
    response = client.post("/api/local-ui/configuration/resume/parse", headers=headers,
                           json={"text": "A resume containing sufficient text for testing."})
    assert response.status_code == 422


def test_parse_retries_missing_evidence_with_larger_budget(owner, monkeypatch):
    client, headers, root, _ = owner
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "true")
    get_settings.cache_clear()
    budgets = []

    class Client:
        def __init__(self, **kwargs):
            budgets.append(kwargs["max_tokens"])

        def complete_structured(self, **kwargs):
            skill = {"value": "Python", "evidence": "使用 Python 开发机器人系统"}
            if len(budgets) == 1:
                skill.pop("evidence")
            return SimpleNamespace(content=json.dumps({"degree": None, "skills": [skill],
                "supporting_skills": [], "projects": []}, ensure_ascii=False))

    monkeypatch.setattr("packages.matching.client.DeepSeekClient", Client)
    response = client.post("/api/local-ui/configuration/resume/parse", headers=headers,
                           json={"text": "使用 Python 开发机器人系统，并负责多个项目的数据处理和接口实现。"})
    assert response.status_code == 200, response.text
    assert budgets == [4000, 8000]
    assert response.json()["draft"]["skills"][0]["evidence"] == "使用 Python 开发机器人系统"
    assert not (root / ".data/settings/candidate_profile.yaml").exists()


def test_parse_reports_truncation_after_bounded_retry(owner, monkeypatch):
    client, headers, root, _ = owner
    monkeypatch.setenv("RECRUITOPS_LLM_ENABLED", "true")
    get_settings.cache_clear()
    budgets = []

    class Client:
        def __init__(self, **kwargs):
            budgets.append(kwargs["max_tokens"])

        def complete_structured(self, **kwargs):
            raise DeepSeekClientError("response_truncated")

    monkeypatch.setattr("packages.matching.client.DeepSeekClient", Client)
    response = client.post("/api/local-ui/configuration/resume/parse", headers=headers,
                           json={"text": "使用 Python 开发机器人系统，并负责多个项目的数据处理和接口实现。"})
    assert response.status_code == 502
    assert "模型没有输出完整简历资料" in response.json()["detail"]
    assert budgets == [4000, 8000]
    assert not (root / ".data/settings/candidate_profile.yaml").exists()
