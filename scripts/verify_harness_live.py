"""Run bounded live acceptance checks against the local Codex Harness.

The default suite is read-only.  It verifies the App Server, RecruitOps MCP,
one real tool call and same-thread context continuity.  Cancellation is opt-in
because it starts a live crawler turn before immediately interrupting it.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


EXPECTED_MCP_VERSION = "14"
EXPECTED_MCP_FULL_TOOL_COUNT = 33
EXPECTED_OC_CANDIDATE_TOOLS = frozenset(
    {"offerbiu_source_refresh"}
)
DEFAULT_API_BASE_URL = "http://127.0.0.1:8012"
DEFAULT_TIMEOUT_SECONDS = 180.0
_TERMINAL_EVENTS = {"turn_completed", "error"}


class HarnessApi(Protocol):
    def health(self) -> dict[str, Any]: ...

    def mcp_status(self) -> dict[str, Any]: ...

    def start_thread(self) -> dict[str, Any]: ...

    def stream_turn(self, thread_id: str, text: str) -> list[dict[str, Any]]: ...

    def start_turn(self, thread_id: str, text: str) -> dict[str, Any]: ...

    def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    evidence: dict[str, Any]


class LocalHarnessApi:
    """Small loopback-only client that deliberately ignores proxy settings."""

    def __init__(self, base_url: str, *, timeout: float) -> None:
        self.base_url = _local_base_url(base_url)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}))

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/api/codex/health")

    def mcp_status(self) -> dict[str, Any]:
        return self._json("GET", "/api/codex/mcp-status")

    def start_thread(self) -> dict[str, Any]:
        return self._json("POST", "/api/codex/threads", {})

    def stream_turn(self, thread_id: str, text: str) -> list[dict[str, Any]]:
        path = f"/api/codex/threads/{thread_id}/turns/stream"
        request = self._request("POST", path, {"text": text})
        events: list[dict[str, Any]] = []
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                event_name = "message"
                data_lines: list[str] = []
                while True:
                    raw = response.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            payload = _json_value("\n".join(data_lines))
                            events.append({"event": event_name, "data": payload})
                            if event_name in _TERMINAL_EVENTS:
                                break
                        event_name = "message"
                        data_lines = []
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("harness stream unavailable") from exc
        return events

    def start_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/codex/threads/{thread_id}/turns",
            {"text": text},
        )

    def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/api/codex/threads/{thread_id}/interrupt",
            {"turn_id": turn_id},
        )

    def _json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request(method, path, body)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = _json_value(response.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("local Harness API unavailable") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("local Harness API returned a non-object")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> Request:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)


def run_acceptance(
    api: HarnessApi,
    *,
    marker: str,
    exercise_cancel: bool = False,
) -> dict[str, Any]:
    checks: list[Check] = []

    health = api.health()
    checks.append(
        Check(
            "runtime_ready",
            health.get("ready") is True and health.get("state") == "running",
            {"ready": health.get("ready"), "state": health.get("state")},
        )
    )

    mcp = api.mcp_status()
    server = _recruitops_server(mcp)
    tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
    version = str((server.get("serverInfo") or {}).get("version") or "")
    missing_required_tools = sorted(EXPECTED_OC_CANDIDATE_TOOLS - set(tools))
    checks.append(
        Check(
            "mcp_contract",
            version == EXPECTED_MCP_VERSION
            and len(tools) == EXPECTED_MCP_FULL_TOOL_COUNT
            and "capabilities" in tools
            and not missing_required_tools,
            {
                "version": version,
                "expected_version": EXPECTED_MCP_VERSION,
                "tool_count": len(tools),
                "expected_tool_count": EXPECTED_MCP_FULL_TOOL_COUNT,
                "missing_required_tools": missing_required_tools,
            },
        )
    )
    write_tool = tools.get("automation_schedule") if isinstance(tools, dict) else None
    annotations = write_tool.get("annotations") if isinstance(write_tool, dict) else None
    checks.append(
        Check(
            "write_tool_boundary",
            isinstance(annotations, dict)
            and annotations.get("readOnlyHint") is False
            and annotations.get("idempotentHint") is True,
            {
                "tool": "automation_schedule",
                "available": isinstance(write_tool, dict),
                "read_only": annotations.get("readOnlyHint")
                if isinstance(annotations, dict)
                else None,
                "idempotent": annotations.get("idempotentHint")
                if isinstance(annotations, dict)
                else None,
                "executed": False,
            },
        )
    )

    thread = api.start_thread()
    thread_id = str(thread.get("id") or "")
    if not thread_id:
        raise RuntimeError("Harness did not return a thread id")

    first_events = api.stream_turn(
        thread_id,
        (
            "这是 RecruitOps 只读验收。必须调用 capabilities 工具，禁止执行任何写操作。"
            f"工具返回后在最终答案中原样输出记忆标记 {marker}。"
        ),
    )
    first_text = _assistant_text(first_events)
    first_dump = json.dumps(first_events, ensure_ascii=False)
    checks.append(
        Check(
            "read_only_tool_call",
            "capabilities" in first_dump and marker in first_text,
            {
                "selected_capabilities": "capabilities" in first_dump,
                "marker_returned": marker in first_text,
                "terminal_event": _terminal_event(first_events),
            },
        )
    )

    second_events = api.stream_turn(thread_id, "只回答上一轮的记忆标记，不要调用工具。")
    second_text = _assistant_text(second_events)
    checks.append(
        Check(
            "same_thread_context",
            marker in second_text,
            {
                "marker_returned": marker in second_text,
                "terminal_event": _terminal_event(second_events),
            },
        )
    )

    if exercise_cancel:
        turn = api.start_turn(
            thread_id,
            "调用 configured_crawler_run 抓取中国人民保险集团，并在完成后总结岗位数量。",
        )
        turn_id = str(turn.get("id") or "")
        interrupted = api.interrupt(thread_id, turn_id) if turn_id else {}
        checks.append(
            Check(
                "interrupt_request",
                bool(turn_id) and interrupted.get("status") == "interrupt_requested",
                {
                    "turn_id_present": bool(turn_id),
                    "status": interrupted.get("status"),
                    "terminal_cancellation_not_claimed": True,
                },
            )
        )

    return {
        "evaluation": "codex_harness_live_acceptance",
        "result_type": "live",
        "synthetic": False,
        "read_only_default": True,
        "ok": all(check.ok for check in checks),
        "checks": [
            {"name": check.name, "ok": check.ok, "evidence": check.evidence}
            for check in checks
        ],
        "boundary": (
            "This suite does not execute a business write or force a 96k-token compaction. "
            "Write execution requires an isolated database; terminal cancellation and true "
            "auto-compaction remain separate live acceptance cases."
        ),
    }


def _local_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("API base URL must be loopback HTTP")
    return candidate


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON from local Harness API") from exc


def _recruitops_server(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("name") or "").casefold() == "recruitops":
                return row
    return {}


def _assistant_text(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict) or data.get("event_type") != "text_delta":
            continue
        text = data.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _terminal_event(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        name = str(event.get("event") or "")
        if name in _TERMINAL_EVENTS:
            return name
    return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Required safety flag.")
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--exercise-cancel",
        action="store_true",
        help="Start a read-only crawler turn and immediately request interruption.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "error": "pass --live"}))
        return 2
    marker = f"ROPS-{secrets.token_hex(6).upper()}"
    try:
        report = run_acceptance(
            LocalHarnessApi(args.api_base_url, timeout=args.timeout),
            marker=marker,
            exercise_cancel=args.exercise_cancel,
        )
    except (RuntimeError, ValueError) as exc:
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
