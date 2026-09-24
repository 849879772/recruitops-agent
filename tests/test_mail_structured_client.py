import json

import pytest

from packages.matching.client import DeepSeekClient, DeepSeekClientError


def test_structured_call_forces_one_named_tool_and_preserves_schema():
    sent = []
    def transport(endpoint, headers, payload, timeout):
        sent.append(payload)
        return {"model": "actual-model", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "name": "submit_mail_analysis", "input": {"result": [{"value": "ok"}]}}]}
    client = DeepSeekClient(api_key="fixture", model="fixture", transport=transport)
    schema = {"type": "array", "$defs": {"row": {"type": "object"}}, "items": {"$ref": "#/$defs/row"}}
    response = client.complete_structured(system_prompt="schema", user_prompt="mail", schema=schema)
    assert json.loads(response.content) == [{"value": "ok"}]
    assert response.model == "actual-model"
    assert sent[0]["tool_choice"] == {"type": "tool", "name": "submit_mail_analysis"}
    assert sent[0]["reasoning"] == {"effort": "none"}
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[0]["tools"][0]["input_schema"]["$defs"] == schema["$defs"]
    assert "$defs" in schema


def test_openai_compatible_structured_call_sends_required_schema_to_model():
    sent = []
    def transport(endpoint, headers, payload, timeout):
        sent.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"value":"ok","evidence":"source"}'}}]}

    schema = {"type": "object", "properties": {
        "value": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["value", "evidence"], "additionalProperties": False}
    client = DeepSeekClient(api_key="fixture", model="fixture", api_style="openai", endpoint="https://gateway.example/v1/chat/completions",
                            transport=transport, max_tokens=8000)
    result = client.complete_structured(system_prompt="Extract JSON", user_prompt="resume",
                                        schema=schema, max_tokens=8000)

    assert json.loads(result.content)["evidence"] == "source"
    assert sent[0]["max_tokens"] == 8000
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert json.dumps(schema, ensure_ascii=False, separators=(",", ":")) in sent[0]["messages"][0]["content"]
    assert "thinking" not in sent[0] and "reasoning_effort" not in sent[0]


@pytest.mark.parametrize("model,endpoint", [
    ("deepseek-flash", "https://gateway.example/v1/chat/completions"),
    ("deepseek/deepseek-v4-pro", "https://gateway.example/v1/chat/completions"),
    ("provider-alias", "https://api.deepseek.com/chat/completions"),
])
def test_openai_deepseek_structured_disables_thinking_even_if_client_enabled(model, endpoint):
    sent = []
    def transport(_endpoint, _headers, payload, _timeout):
        sent.append(payload)
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    client = DeepSeekClient(api_key="fixture", model=model, endpoint=endpoint,
                           api_style="openai", thinking_enabled=True, transport=transport)
    client.complete_structured(system_prompt="schema", user_prompt="resume", schema={"type": "object"})
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in sent[0] and "reasoning" not in sent[0]
    client.complete_with_thinking(system_prompt="match", user_prompt="job")
    assert sent[1]["thinking"] == {"type": "enabled"}
    assert sent[1]["reasoning_effort"] == "high"


def test_deepseek_thinking_rejection_does_not_silently_drop_parameter():
    calls = []
    def transport(_endpoint, _headers, payload, _timeout):
        calls.append(payload)
        raise DeepSeekClientError("http_400")
    client = DeepSeekClient(api_key="fixture", model="deepseek-flash", api_style="openai", transport=transport)
    with pytest.raises(DeepSeekClientError, match="http_400"):
        client.complete_structured(system_prompt="schema", user_prompt="resume", schema={"type": "object"})
    assert len(calls) == 1
    assert calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("blocks,stop", [
    ([{"type": "text", "text": '{"result": {}}'}], "end_turn"),
    ([{"type": "tool_use", "name": "wrong", "input": {"result": {}}}], "tool_use"),
    ([{"type": "tool_use", "name": "submit_mail_analysis", "input": {"result": {}}}], "max_tokens"),
    ([{"type": "tool_use", "name": "submit_mail_analysis", "input": {"result": {}}}] * 2, "tool_use"),
])
def test_structured_call_rejects_invalid_or_truncated_calls_without_retry(blocks, stop):
    calls = []
    def transport(*args):
        calls.append(1)
        return {"content": blocks, "stop_reason": stop}
    client = DeepSeekClient(api_key="fixture", model="fixture", transport=transport)
    with pytest.raises(DeepSeekClientError, match="structured_response_invalid"):
        client.complete_structured(system_prompt="schema", user_prompt="mail", schema={"type": "object"})
    assert len(calls) == 1
