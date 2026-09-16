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
