import json
from copy import deepcopy
import pytest
from packages.matching.client import DeepSeekClient, DeepSeekClientError
from packages.model_policy import DEEPSEEK_RESPONSES


def completed(result):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"result": result})}]}]}


def test_schema_is_sent_to_server_without_mutation():
    sent = []
    def transport(endpoint, headers, payload, timeout):
        assert endpoint == DEEPSEEK_RESPONSES
        assert headers["Authorization"] == "Bearer fixture"
        sent.append(payload)
        return completed([{"value": "ok"}])
    schema = {"type": "array", "$defs": {"row": {"type": "object", "properties": {
        "value": {"type": "string", "default": ""}}}}, "items": {"$ref": "#/$defs/row"}}
    before = deepcopy(schema)
    client = DeepSeekClient(api_key="fixture", model="deepseek-flash", transport=transport)
    result = client.complete_structured(system_prompt="schema", user_prompt="mail", schema=schema)
    assert json.loads(result.content) == [{"value": "ok"}]
    format_ = sent[0]["text"]["format"]
    assert format_["type"] == "json_schema"
    assert format_["schema"]["$defs"]["row"]["required"] == ["value"]
    assert format_["schema"]["$defs"]["row"]["additionalProperties"] is False
    assert "default" not in format_["schema"]["$defs"]["row"]["properties"]["value"]
    assert schema == before


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_extraction_disables_thinking_and_matching_can_keep_it(model):
    sent = []
    def transport(endpoint, headers, payload, timeout):
        sent.append(payload)
        return completed({})
    client = DeepSeekClient(api_key="fixture", model=model, thinking_enabled=True, transport=transport)
    client.complete_structured(system_prompt="schema", user_prompt="resume", schema={"type": "object"})
    assert sent[0]["reasoning"] == {"effort": "none"}
    client.complete_structured(system_prompt="schema", user_prompt="job", schema={"type": "object"},
                               thinking_enabled=True, max_tokens=8000)
    assert sent[1]["reasoning"] == {"effort": "high"}
    assert sent[1]["max_output_tokens"] == 8000


@pytest.mark.parametrize("kwargs", [
    {"model": "alias"},
    {"model": "deepseek-flash", "api_style": "openai"},
    {"model": "deepseek-flash", "endpoint": "https://gateway.example/v1/chat/completions"},
    {"model": "deepseek-flash", "endpoint": "https://api.deepseek.com.evil.test/responses"},
])
def test_unapproved_provider_rejected_before_network(kwargs):
    with pytest.raises(ValueError):
        DeepSeekClient(api_key="fixture", transport=lambda *args: pytest.fail("network"), **kwargs)


def test_schema_rejection_has_no_weaker_fallback():
    calls = []
    def transport(*args):
        calls.append(args)
        raise DeepSeekClientError("http_400")
    client = DeepSeekClient(api_key="fixture", model="deepseek-flash", transport=transport)
    with pytest.raises(DeepSeekClientError, match="http_400"):
        client.complete_structured(system_prompt="schema", user_prompt="resume", schema={"type": "object"})
    assert len(calls) == 1


@pytest.mark.parametrize("response,code", [
    ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "response_truncated"),
    ({"status": "failed"}, "response_invalid"),
    ({"status": "completed", "output": None}, "response_invalid"),
    ({"status": "completed", "output": [{"type": "message", "content": None}]}, "response_invalid"),
    ({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": None}]}]}, "response_invalid"),
    ({"status": "completed", "output": []}, "response_empty"),
    ({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "no"}]}]}, "response_refused"),
    ({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "{}"}]}]}, "structured_response_invalid"),
])
def test_bad_response_is_not_accepted(response, code):
    client = DeepSeekClient(api_key="fixture", model="deepseek-flash",
                            max_attempts=1, transport=lambda *args: response)
    with pytest.raises(DeepSeekClientError, match=code):
        client.complete_structured(system_prompt="schema", user_prompt="mail", schema={"type": "object"})


def test_partial_plain_text_rejected():
    client = DeepSeekClient(api_key="fixture", model="deepseek-flash", transport=lambda *args: {
        "stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]})
    with pytest.raises(DeepSeekClientError, match="response_truncated"):
        client.complete(system_prompt="test", user_prompt="test")
