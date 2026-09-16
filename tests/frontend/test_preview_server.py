from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from preview_server import PreviewHTTPServer


class RecordingUpstream(BaseHTTPRequestHandler):
    calls: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        self.calls.append((self.command, self.path))
        body = b'{"upstream":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self.calls.append((self.command, self.path))
        self.send_response(500)
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        self.calls.append((self.command, self.path))
        self.send_response(500)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def servers():
    RecordingUpstream.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
    preview = PreviewHTTPServer(
        port=0,
        web_root=Path(__file__).parents[2] / "apps" / "web",
        upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
    )
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (upstream, preview)]
    for thread in threads:
        thread.start()
    yield preview, upstream
    preview.shutdown()
    upstream.shutdown()
    preview.server_close()
    upstream.server_close()


def request(preview: PreviewHTTPServer, path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, dict | bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{preview.server_address[1]}{path}", method=method, data=body)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_only_explicit_read_only_get_is_proxied_and_mail_is_mocked(servers):
    preview, _upstream = servers
    status, payload = request(preview, "/api/jobs/browse?limit=1")
    assert status == 200
    assert payload == {"upstream": True}
    assert RecordingUpstream.calls == [("GET", "/api/jobs/browse?limit=1")]
    status, payload = request(preview, "/api/recruitment-mails?limit=50")
    assert status == 200
    assert payload["simulated"] is True
    assert RecordingUpstream.calls == [("GET", "/api/jobs/browse?limit=1")]


def test_company_sources_are_always_mocked_with_retry_and_conflict(servers):
    preview, _upstream = servers
    status, listing = request(preview, "/api/company-sources?page=1&page_size=30&status=failed")
    assert status == 200 and listing["total"] == 1
    source = listing["items"][0]
    assert source["last_success_job_count"] == 12 and source["job_count"] == 0
    base = f'/api/company-sources/{source["id"]}'
    status, _ = request(preview, base + "/entry", "PATCH", json.dumps({"entry_url": "https://example.test/new", "expected_updated_at": "stale"}).encode())
    assert status == 409
    status, changed = request(preview, base + "/entry", "PATCH", json.dumps({"entry_url": "https://example.test/new", "expected_updated_at": source["updated_at"]}).encode())
    assert status == 200 and changed["entry_url"] == "https://example.test/new"
    status, started = request(preview, base + "/retry", "POST", b"{}")
    assert status == 202 and started["status"] == "running"
    assert request(preview, base)[1]["status"] == "running"
    assert request(preview, base)[1]["status"] == "running"
    assert request(preview, base)[1]["status"] == "complete"
    assert RecordingUpstream.calls == []

@pytest.mark.parametrize("path", [
    "/api/approvals/abc/approve",
    "/api/recruitment-mails/sync",
    "/api/codex/threads",
    "/api/unknown-write",
])
def test_non_get_never_writes_to_upstream(servers, path):
    preview, _upstream = servers
    method = "POST" if path != "/api/codex/threads" else "POST"
    status, _payload = request(preview, path, method, b"{}")
    assert status in {200, 404, 405}
    assert not [call for call in RecordingUpstream.calls if call[0] != "GET"]


def test_codex_mock_create_resume_stream_stop_delete_success_path(servers):
    preview, _upstream = servers
    status, created = request(preview, "/api/codex/threads", "POST", b"{}")
    assert status == 200 and created["simulated"] is True
    thread_id = created["id"]
    status, resumed = request(preview, f"/api/codex/threads/{thread_id}/resume", "POST", b"{}")
    assert status == 200 and resumed["id"] == thread_id
    status, stream = request(preview, f"/api/codex/threads/{thread_id}/turns/stream", "POST", json.dumps({"text": "测试"}).encode())
    assert status == 200 and b"turn_completed" in stream
    status, deleted = request(preview, f"/api/codex/threads/{thread_id}", "DELETE")
    assert status == 200 and deleted["simulated"] is True
    assert RecordingUpstream.calls == []


def test_pure_mock_port_mode_does_not_proxy_read_only_get(servers):
    _preview, upstream = servers
    preview = PreviewHTTPServer(port=0, web_root=Path(__file__).parents[2] / "apps" / "web", upstream=f"http://127.0.0.1:{upstream.server_address[1]}", pure_mock=True)
    thread = threading.Thread(target=preview.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = request(preview, "/api/jobs/browse")
        assert status == 200 and payload["simulated"] is True
        assert RecordingUpstream.calls == []
    finally:
        preview.shutdown()
        preview.server_close()


def test_pure_mock_has_ui_contracts_mail_fixture_and_local_editing(servers):
    _preview, upstream = servers
    preview = PreviewHTTPServer(port=0, web_root=Path(__file__).parents[2] / "apps" / "web", upstream=f"http://127.0.0.1:{upstream.server_address[1]}", pure_mock=True)
    thread = threading.Thread(target=preview.serve_forever, daemon=True)
    thread.start()
    try:
        load_core = [
            ("/health", dict),
            ("/api/codex/health", dict),
            ("/api/schedule?on=2026-09-08", list),
            ("/api/jobs?cohort=2027&limit=8", dict),
            ("/api/companies", list),
            ("/api/approvals", list),
            ("/api/codex/traces", list),
            ("/api/recruitment-mails?limit=50", dict),
            ("/api/reports/operational?on=2026-09-08", dict),
        ]
        for path, expected_type in load_core:
            assert isinstance(request(preview, path)[1], expected_type), path
        jobs = request(preview, "/api/jobs/browse")[1]
        assert {"stats", "facets", "featured"} <= jobs.keys()
        mails = request(preview, "/api/recruitment-mails")[1]["items"]
        assert {item["processing_status"] for item in mails} == {"processed_unchanged", "pending_association"}
        assert mails[0]["application_id"] == "preview-application-1"
        patch_status, patched = request(preview, "/api/local-ui/applications/preview-application-1", "PATCH", json.dumps({"note": "已编辑"}).encode())
        assert patch_status == 200 and patched["note"] == "已编辑"
        schedule_status, schedule = request(preview, "/api/local-ui/applications/preview-application-1/events", "POST", json.dumps({"event_type": "测评", "event_date": "2026-09-09"}).encode())
        assert schedule_status == 200 and schedule["simulated"] is True
        delete_status, deleted = request(preview, "/api/local-ui/applications/preview-application-1", "DELETE")
        assert delete_status == 200 and deleted["deleted"] is True
        assert RecordingUpstream.calls == []
    finally:
        preview.shutdown()
        preview.server_close()


def test_static_preview_banner_and_delayed_stop_stream(servers):
    _preview, upstream = servers
    preview = PreviewHTTPServer(port=0, web_root=Path(__file__).parents[2] / "apps" / "web", upstream=f"http://127.0.0.1:{upstream.server_address[1]}", pure_mock=True)
    server_thread = threading.Thread(target=preview.serve_forever, daemon=True)
    server_thread.start()
    try:
        status, html = request(preview, "/")
        assert status == 200 and "预览模式：全部数据为模拟".encode() in html
        assert html.index("预览模式".encode()) > html.lower().index(b"<body")
        request(preview, "/api/codex/threads", "POST", b"{}")
        thread_id = next(iter(preview.mock_state.threads))
        result: list[tuple[int, dict | bytes]] = []
        stream_thread = threading.Thread(target=lambda: result.append(request(preview, f"/api/codex/threads/{thread_id}/turns/stream", "POST", json.dumps({"text": "停止测试"}).encode())), daemon=True)
        stream_thread.start()
        time.sleep(0.25)
        stop_status, stop = request(preview, f"/api/codex/threads/{thread_id}/stop", "POST", b"{}")
        stream_thread.join(timeout=4)
        assert stop_status == 200 and stop["simulated"] is True
        assert result and b"event: turn" in result[0][1]
        detail_status, detail = request(preview, f"/api/codex/threads/{thread_id}")
        assert detail_status == 200
        assert [item["type"] for item in detail["turns"][0]["items"]] == ["user", "assistant"]
    finally:
        preview.shutdown()
        preview.server_close()
