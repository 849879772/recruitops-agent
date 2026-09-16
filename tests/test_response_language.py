import asyncio
from packages.codex_runtime.threads import CodexThreads
from packages.codex_runtime.instructions import RESPONSE_LANGUAGE_INSTRUCTIONS, with_response_language


def test_language_applies_to_new_and_resumed_threads_without_rewriting_user_text():
    class Supervisor:
        def __init__(self):
            self.calls = []
        async def request(self, method, params):
            self.calls.append((method, params))
            return {"turn" if method == "turn/start" else "thread": {"id": "example"}}
    supervisor = Supervisor()
    threads = CodexThreads(supervisor)
    async def run():
        await threads.start(developerInstructions="Keep existing safety rules")
        await threads.resume("example")
        await threads.start_turn("example", "处理邮箱")
    asyncio.run(run())
    for _, params in supervisor.calls[:2]:
        assert RESPONSE_LANGUAGE_INSTRUCTIONS in params["developerInstructions"]
    assert "Keep existing safety rules" in supervisor.calls[0][1]["developerInstructions"]
    assert supervisor.calls[2][1]["input"] == [{"type":"text","text":"处理邮箱"}]
    values = with_response_language(with_response_language({}))
    assert values["developerInstructions"].count(RESPONSE_LANGUAGE_INSTRUCTIONS) == 1


def test_rule_covers_progress_and_preserves_protocol():
    for phrase in ("commentary", "开场", "重试", "最终", "JSON", "保持原样"):
        assert phrase in RESPONSE_LANGUAGE_INSTRUCTIONS
