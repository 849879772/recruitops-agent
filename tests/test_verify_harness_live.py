from __future__ import annotations

import pytest

from scripts.verify_harness_live import (
    EXPECTED_MCP_FULL_TOOL_COUNT,
    EXPECTED_OC_CANDIDATE_TOOLS,
    _local_base_url,
    run_acceptance,
)


class FakeHarnessApi:
    def __init__(self, *, keep_context: bool = True) -> None:
        self.keep_context = keep_context
        self.turns = 0

    def health(self):
        return {"ready": True, "state": "running"}

    def mcp_status(self):
        tools = {
            "capabilities": {},
            "automation_schedule": {
                "annotations": {
                    "readOnlyHint": False,
                    "idempotentHint": True,
                }
            },
            **{name: {} for name in EXPECTED_OC_CANDIDATE_TOOLS},
        }
        tools.update(
            {
                f"fixture_tool_{index}": {}
                for index in range(EXPECTED_MCP_FULL_TOOL_COUNT - len(tools))
            }
        )
        return {
            "data": [
                {
                    "name": "recruitops",
                    "serverInfo": {"version": "14"},
                    "tools": tools,
                }
            ]
        }

    def start_thread(self):
        return {"id": "thread-1"}

    def stream_turn(self, thread_id, text):
        assert thread_id == "thread-1"
        self.turns += 1
        marker = "ROPS-ABC123" if self.turns == 1 or self.keep_context else "forgot"
        events = []
        if self.turns == 1:
            events.append(
                {
                    "event": "item_started",
                    "data": {
                        "event_type": "item_started",
                        "payload": {"tool": "capabilities"},
                    },
                }
            )
        events.extend(
            [
                {
                    "event": "text_delta",
                    "data": {"event_type": "text_delta", "text": marker},
                },
                {
                    "event": "turn_completed",
                    "data": {"event_type": "turn_completed"},
                },
            ]
        )
        return events

    def start_turn(self, thread_id, text):
        return {"id": "turn-cancel"}

    def interrupt(self, thread_id, turn_id):
        return {"status": "interrupt_requested"}


def test_acceptance_covers_tool_context_write_boundary_and_interrupt() -> None:
    report = run_acceptance(
        FakeHarnessApi(),
        marker="ROPS-ABC123",
        exercise_cancel=True,
    )

    assert report["ok"] is True
    assert [item["name"] for item in report["checks"]] == [
        "runtime_ready",
        "mcp_contract",
        "write_tool_boundary",
        "read_only_tool_call",
        "same_thread_context",
        "interrupt_request",
    ]
    assert report["checks"][2]["evidence"]["executed"] is False
    assert report["checks"][1]["evidence"]["tool_count"] == 33
    assert report["checks"][1]["evidence"]["missing_required_tools"] == []
    assert "does not execute a business write" in report["boundary"]


def test_context_loss_fails_without_hiding_other_checks() -> None:
    report = run_acceptance(
        FakeHarnessApi(keep_context=False),
        marker="ROPS-ABC123",
    )

    assert report["ok"] is False
    failed = [item["name"] for item in report["checks"] if not item["ok"]]
    assert failed == ["same_thread_context"]


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8012",
        "http://example.com:8012",
        "http://user:password@127.0.0.1:8012",
        "http://127.0.0.1:8012/api",
    ],
)
def test_local_client_rejects_non_loopback_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValueError):
        _local_base_url(url)
