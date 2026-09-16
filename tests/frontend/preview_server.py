"""Loopback-only preview server for the new Web UI.

This is intentionally separate from the production API.  The only requests that
can reach port 8012 are explicit, read-only GET routes.  Mail and every Codex
route are always served from the in-memory simulated fixtures below.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_PREVIEW_PORT = 8899
PURE_MOCK_PORT = 8898
UPSTREAM = "http://127.0.0.1:8012"

# Keep this list explicit.  In particular, do not add a prefix-wide /api rule:
# a future write endpoint must fail closed until it has been reviewed here.
READ_ONLY_GETS = {
    "/health",
    "/api/jobs",
    "/api/jobs/browse",
    "/api/companies",
    "/api/applications/page",
    "/api/schedule",
    "/api/automations",
    "/api/approvals",
    "/api/reports/operational",
}


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class MockState:
    """Small per-server fixture store; all values are explicitly simulated."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.threads: dict[str, dict[str, Any]] = {}
        self.next_thread = 1
        self.next_turn = 1
        self.applications: dict[str, dict[str, Any]] = {
            "preview-application-1": {
                "id": "preview-application-1", "company_name": "预览科技", "job_title": "算法工程师",
                "stage": "applied", "note": "纯 mock 投递记录", "updated_at": "2026-09-08T09:00:00Z",
                "stage_history": [{"stage": "applied", "result": "已投递", "changed_at": "2026-09-08"}], "simulated": True,
            },
        }
        self.schedules: list[dict[str, Any]] = []
        self.company_sources = {}
        self.source_retry_reads: dict[str, int] = {}
        for index, status in enumerate(("failed", "partial", "pending", "unusable", "complete", "running"), 1):
            source_id = f"preview-source-{index}"
            self.company_sources[source_id] = {
                "id": source_id, "company_name": f"模拟来源公司{index}", "source": "OC（模拟）",
                "source_record_id": f"fixture-{index}", "source_url": f"https://example.test/source/{index}",
                "entry_url": f"https://example.test/campus/{index}", "original_entry_url": f"https://example.test/original/{index}",
                "final_url": None if status in {"pending", "unusable"} else f"https://example.test/jobs/{index}",
                "status": status, "failure_stage": "detail" if status == "partial" else "entry" if status in {"failed", "unusable"} else None,
                "reason_code": "preview_timeout" if status == "failed" else "preview_entry_unavailable" if status == "unusable" else None,
                "reason": "模拟诊断：入口超时，历史成功岗位仍保留" if status == "failed" else "模拟诊断：部分JD尚未完成" if status == "partial" else None,
                "job_count": 8 if status in {"complete", "partial"} else 0,
                "jd_pending_count": 3 if status == "partial" else 0,
                "last_success_job_count": 12 if status == "failed" else 8 if status in {"complete", "partial"} else None,
                "pagination_complete": True if status == "complete" else None if status == "pending" else False,
                "last_attempt_at": None if status == "pending" else "2026-09-08T10:00:00Z",
                "updated_at": "2026-09-08T10:00:00Z", "simulated": True,
            }

    def create_thread(self) -> dict[str, Any]:
        with self.lock:
            thread_id = f"preview-thread-{self.next_thread}"
            self.next_thread += 1
            thread = {
                "id": thread_id,
                "title": "预览模拟会话",
                "turns": [],
                "simulated": True,
            }
            self.threads[thread_id] = thread
            return thread

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self.lock:
            thread = self.threads.get(thread_id)
            return json.loads(json.dumps(thread)) if thread else None

    def run_turn(self, thread_id: str, text: str) -> tuple[str, str] | None:
        with self.lock:
            thread = self.threads.get(thread_id)
            if not thread:
                return None
            turn_id = f"preview-turn-{self.next_turn}"
            self.next_turn += 1
            thread["turns"].append({
                "id": turn_id,
                "items": [
                    {"type": "user", "text": text},
                    {"type": "assistant", "text": "这是预览环境中的模拟回答。"},
                ],
            })
            return turn_id, "这是预览环境中的模拟回答。"

    def patch_application(self, application_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            application = self.applications.get(application_id)
            if not application:
                return None
            application.update({key: value for key, value in payload.items() if key in {"stage", "result", "note"}})
            return json.loads(json.dumps(application))


def is_read_only_get(path: str) -> bool:
    """Match exact reviewed routes plus only the reviewed detail subroutes."""
    if path in READ_ONLY_GETS:
        return True
    return bool(re.fullmatch(r"/api/jobs/[^/]+", path))


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "RecruitOpsPreview/1"
    protocol_version = "HTTP/1.1"

    @property
    def preview(self) -> "PreviewHTTPServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *args: Any) -> None:
        # Avoid access logs containing query/body data (mail content must never log).
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/company-sources" or parsed.path.startswith("/api/company-sources/"):
            self._mock_company_sources(parsed.path, parsed.query)
            return
        if parsed.path.startswith("/api/codex/") or parsed.path == "/api/recruitment-mails" or parsed.path.startswith("/api/recruitment-mails/"):
            self._mock_api(parsed.path, parsed.query)
            return
        if self._mock_mode(parsed.query):
            if parsed.path.startswith("/api/") or parsed.path == "/health":
                self._mock_api(parsed.path, parsed.query)
                return
        if parsed.path.startswith("/api/") or parsed.path == "/health":
            if not is_read_only_get(parsed.path):
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "preview route is not in the read-only allowlist"})
                return
            self._proxy_get(parsed.path, parsed.query)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        self._request_payload = self._read_json_body()
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/api/company-sources/"):
            self._mock_company_sources(parsed.path, parsed.query, self._request_payload)
            return
        event = re.fullmatch(r"/api/local-ui/applications/([^/]+)/events", parsed.path)
        if self._mock_mode(parsed.query) and event:
            application_id = urllib.parse.unquote(event.group(1))
            application = self.preview.mock_state.applications.get(application_id, {})
            item = {**self._request_payload, "application_id": application_id, "company_name": application.get("company_name", "模拟公司"), "job_title": application.get("job_title", "模拟岗位"), "id": f"preview-schedule-{len(self.preview.mock_state.schedules) + 1}", "simulated": True}
            self.preview.mock_state.schedules.append(item)
            self._send_json(HTTPStatus.OK, item)
            return
        if parsed.path.startswith("/api/codex/") or parsed.path.startswith("/api/recruitment-mails"):
            self._mock_api(parsed.path, parsed.query)
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"detail": "preview never forwards non-GET requests"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._request_payload = self._read_json_body()
        parsed = urllib.parse.urlsplit(self.path)
        if self._mock_mode(parsed.query):
            match = re.fullmatch(r"/api/local-ui/applications/([^/]+)", parsed.path)
            if match:
                deleted = self.preview.mock_state.applications.pop(urllib.parse.unquote(match.group(1)), None)
                self._send_json(HTTPStatus.OK, {"deleted": deleted is not None, "simulated": True})
                return
        if parsed.path.startswith("/api/codex/"):
            self._mock_api(parsed.path, parsed.query)
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"detail": "preview never forwards non-GET requests"})

    def do_PATCH(self) -> None:  # noqa: N802
        payload = self._read_json_body()
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/api/company-sources/"):
            self._mock_company_sources(parsed.path, parsed.query, payload)
            return
        match = re.fullmatch(r"/api/local-ui/applications/([^/]+)", parsed.path)
        if self._mock_mode(parsed.query) and match:
            application = self.preview.mock_state.patch_application(urllib.parse.unquote(match.group(1)), payload)
            self._send_json(HTTPStatus.OK if application else HTTPStatus.NOT_FOUND, application or {"detail": "simulated application not found"})
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"detail": "preview never forwards non-GET requests"})

    def _mock_company_sources(self, path: str, query: str, payload: dict[str, Any] | None = None) -> None:
        state = self.preview.mock_state
        params = urllib.parse.parse_qs(query)
        if path == "/api/company-sources" and self.command == "GET":
            q = params.get("q", [""])[0].casefold()
            status = params.get("status", [""])[0]
            try:
                page = max(1, int(params.get("page", ["1"])[0]))
                size = min(100, max(1, int(params.get("page_size", ["30"])[0])))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "invalid pagination"})
                return
            rows = [row for row in state.company_sources.values() if q in row["company_name"].casefold() and (not status or row["status"] == status)]
            self._send_json(HTTPStatus.OK, {"items": rows[(page - 1) * size:page * size], "total": len(rows), "page": page, "page_size": size, "simulated": True})
            return
        match = re.fullmatch(r"/api/company-sources/([^/]+)(?:/(entry|retry))?", path)
        row = state.company_sources.get(urllib.parse.unquote(match.group(1))) if match else None
        if row is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "simulated source not found"})
            return
        action = match.group(2)
        if self.command == "GET" and action is None:
            remaining = state.source_retry_reads.get(row["id"], 0)
            if remaining:
                state.source_retry_reads[row["id"]] = remaining - 1
                if remaining == 1:
                    row.update(status="complete", job_count=8, jd_pending_count=0, last_success_job_count=8, pagination_complete=True, failure_stage=None, reason_code=None, reason=None)
            self._send_json(HTTPStatus.OK, {**row, "attempts": []})
        elif self.command == "PATCH" and action == "entry":
            payload = payload or {}
            if payload.get("expected_updated_at") != row["updated_at"]:
                self._send_json(HTTPStatus.CONFLICT, {"detail": "来源已变化，请刷新后重试"})
                return
            entry = urllib.parse.urlsplit(str(payload.get("entry_url", "")))
            if entry.scheme not in {"http", "https"} or not entry.netloc or entry.username or entry.password:
                self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "请输入公开HTTP(S)入口"})
                return
            row.update(entry_url=payload["entry_url"], updated_at=datetime.now(timezone.utc).isoformat())
            self._send_json(HTTPStatus.OK, row)
        elif self.command == "POST" and action == "retry":
            if row["id"] == "preview-source-4":
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"detail": "模拟：重试服务暂不可用，未启动抓取", "simulated": True})
                return
            row.update(status="running", last_attempt_at="2026-09-08T11:00:00Z")
            state.source_retry_reads[row["id"]] = 3
            self._send_json(HTTPStatus.ACCEPTED, {"id": row["id"], "status": "running", "simulated": True})
        else:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"detail": "unsupported simulated operation"})

    def _mock_mode(self, query: str) -> bool:
        return self.preview.pure_mock or query in {"preview/mock", "preview=mock"} or urllib.parse.parse_qs(query).get("preview") == ["mock"]

    def _proxy_get(self, path: str, query: str) -> None:
        target = f"{self.preview.upstream}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(target, method="GET", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "application/json")
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (urllib.error.URLError, TimeoutError) as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": f"preview upstream unavailable: {exc.__class__.__name__}"})

    def _mock_api(self, path: str, _query: str) -> None:
        state = self.preview.mock_state
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "mode": "preview-mock", "simulated": True})
            return
        if path == "/api/recruitment-mails" or path.startswith("/api/recruitment-mails/"):
            self._send_json(HTTPStatus.OK, {
                "items": [
                    {"id": "preview-mail-applied", "subject": "预览：申请状态更新", "company_name": "预览科技", "processing_status": "processed_unchanged", "application_id": "preview-application-1", "application_status": "applied", "simulated": True},
                    {"id": "preview-mail-pending", "subject": "预览：待关联招聘邮件", "company_name": "待确认公司", "processing_status": "pending_association", "requires_confirmation": True, "application_status": "pending", "simulated": True},
                ], "simulated": True,
                "freshness": {"status": "simulated", "source": "preview fixture"},
            })
            return
        if path == "/api/codex/health":
            self._send_json(HTTPStatus.OK, {"enabled": True, "ready": True, "state": "simulated", "simulated": True})
            return
        if path == "/api/codex/threads" and self.command == "GET":
            self._send_json(HTTPStatus.OK, {"data": list(state.threads.values()), "next_cursor": None, "simulated": True})
            return
        match = re.fullmatch(r"/api/codex/threads/([^/]+)", path)
        if path == "/api/codex/threads" and self.command == "POST":
            self._send_json(HTTPStatus.OK, state.create_thread())
            return
        if match:
            thread_id = urllib.parse.unquote(match.group(1))
            if self.command == "DELETE":
                with state.lock:
                    state.threads.pop(thread_id, None)
                self._send_json(HTTPStatus.OK, {"deleted": True, "thread_id": thread_id, "simulated": True})
                return
            thread = state.get_thread(thread_id)
            if thread is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "simulated thread not found", "simulated": True})
            else:
                self._send_json(HTTPStatus.OK, thread)
            return
        resume = re.fullmatch(r"/api/codex/threads/([^/]+)/resume", path)
        if resume:
            thread = state.get_thread(urllib.parse.unquote(resume.group(1)))
            self._send_json(HTTPStatus.OK if thread else HTTPStatus.NOT_FOUND, thread or {"detail": "simulated thread not found"})
            return
        interrupt = re.fullmatch(r"/api/codex/threads/([^/]+)/(?:interrupt|stop)", path)
        if interrupt:
            self._send_json(HTTPStatus.OK, {"status": "stopped", "simulated": True})
            return
        stream = re.fullmatch(r"/api/codex/threads/([^/]+)/turns/stream", path)
        if stream:
            thread_id = urllib.parse.unquote(stream.group(1))
            payload = getattr(self, "_request_payload", {})
            result = state.run_turn(thread_id, str(payload.get("text", "")))
            if result is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "simulated thread not found"})
                return
            turn_id, answer = result
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            first = {"id": turn_id, "thread_id": thread_id, "event_id": f"{turn_id}-start", "simulated": True}
            self.wfile.write(f"event: turn\ndata: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            if str(payload.get("text", "")) == "停止测试":
                time.sleep(3)
            events = [
                {"event_type": "turn_started", "event_id": f"{turn_id}-started", "thread_id": thread_id, "turn_id": turn_id, "simulated": True},
                {"event_type": "text_delta", "event_id": f"{turn_id}-text", "thread_id": thread_id, "turn_id": turn_id, "text": answer, "simulated": True},
                {"event_type": "turn_completed", "event_id": f"{turn_id}-completed", "thread_id": thread_id, "turn_id": turn_id, "simulated": True},
            ]
            self.wfile.write(b"".join(f"event: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8") for event in events))
            self.wfile.flush()
            return
        events = re.fullmatch(r"/api/codex/threads/([^/]+)/events", path)
        if events:
            self._send_sse([{ "event_type": "turn_completed", "simulated": True }])
            return
        if path == "/api/codex/traces":
            self._send_json(HTTPStatus.OK, [])
            return
        fixtures: dict[str, Any] = {
            "/api/companies": [],
            "/api/schedule": list(state.schedules),
            "/api/approvals": [],
            "/api/codex/traces": [],
            "/api/applications/page": {"items": list(state.applications.values()), "total": len(state.applications)},
            "/api/jobs": {"items": [], "total": 0},
            "/api/jobs/browse": {
                "items": [], "total": 0,
                "stats": {"jobs": 0, "companies": 0, "high_match": 0, "pending": 0, "jd_incomplete": 0, "excluded": 0},
                "facets": {"companies": [], "categories": {}, "platforms": []}, "featured": [],
            },
        }
        fixture = fixtures.get(path, {"items": [], "data": []})
        if isinstance(fixture, list):
            self._send_json(HTTPStatus.OK, fixture)
        else:
            self._send_json(HTTPStatus.OK, {**fixture, "simulated": True})

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_sse(self, events: list[dict[str, Any]]) -> None:
        body = b"".join(f"event: {item.get('event_type', 'message')}\ndata: {json.dumps(item)}\n\n".encode() for item in events)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        relative = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        candidate = (self.preview.web_root / relative).resolve()
        if self.preview.web_root not in candidate.parents and candidate != self.preview.web_root:
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        if not candidate.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        body = candidate.read_bytes()
        if candidate.name == "index.html":
            label = "预览模式：全部数据为模拟（含公司来源、邮件及助理）" if self.preview.pure_mock else "预览模式：公司来源、招聘邮件与助理均为模拟；其余白名单 GET 只读代理"
            banner = (f'<div id="recruitops-preview-banner" style="position:relative;z-index:1;width:100%;box-sizing:border-box;padding:6px 12px;background:#fff3cd;color:#664d03;border-bottom:1px solid #ffda6a;font:12px/1.4 system-ui,sans-serif;text-align:center">{label}</div>').encode("utf-8")
            body = re.sub(rb"(<body\b[^>]*>)", rb"\1" + banner, body, count=1, flags=re.IGNORECASE)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PreviewHTTPServer(ThreadingHTTPServer):
    def __init__(self, port: int = DEFAULT_PREVIEW_PORT, *, web_root: Path | None = None, upstream: str = UPSTREAM, pure_mock: bool = False) -> None:
        self.web_root = (web_root or Path(__file__).resolve().parents[2] / "apps" / "web").resolve()
        self.upstream = upstream.rstrip("/")
        self.pure_mock = pure_mock
        self.mock_state = MockState()
        super().__init__(("127.0.0.1", port), PreviewHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the loopback RecruitOps Web preview server")
    parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT)
    parser.add_argument("--mock", action="store_true", help=f"pure mock API mode (recommended port {PURE_MOCK_PORT})")
    args = parser.parse_args()
    server = PreviewHTTPServer(port=args.port, pure_mock=args.mock or args.port == PURE_MOCK_PORT)
    print(f"RecruitOps preview listening on http://127.0.0.1:{args.port} (simulated APIs marked in responses)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
