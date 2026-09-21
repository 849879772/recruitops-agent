"""Owned protocol/HTTP/WS fixture, NOT a database or real business acceptance."""
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mode = sys.argv[1]
writes = mode in {"writes", "desktop", "delayed-desktop", "filler"} or ("--enable-writes-for-instance" in sys.argv and sys.argv[-1] == "fixture-instance")
fixture_instance = "a" * 32 if mode == "filler" else "fixture-instance"
token = os.environ["RECRUITOPS_DESKTOP_SHELL_TOKEN"]
run_id = "fixture-run"
sequence = 0
emit_lock = threading.Lock()
application_requests = []


def emit(event, stage, **fields):
    global sequence
    with emit_lock:
        sequence += 1
        print(json.dumps(dict(protocol=1, sequence=sequence, run_id=run_id,
                             event=event, stage=stage, **fields)), flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + token:
            self.send_error(401)
            return
        if self.path == "/desktop-runtime/ready":
            self.send_json(dict(status="ready", instance_id="foreign" if mode == "identity-failure" else fixture_instance,
                                run_id=run_id, writes=writes, websocket=writes))
        elif self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            if not writes:
                self.send_error(403)
                return
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            self.wfile.write(b"\x81\x07fixture")
            self.wfile.flush()
            self.close_connection = True
        elif self.path == "/api/read":
            self.send_json({"fixture": True, "read": True})
        elif mode == "filler" and self.path == "/fixture/application-requests":
            self.send_json(application_requests)
        elif mode == "filler" and self.path == "/api/integrations/resume-filler/applications":
            application_requests.append({"method": "GET", "path": self.path})
            self.send_json({"items": []})
        else:
            if mode == "delayed-desktop":
                time.sleep(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<!doctype html><title>Owned fixture workbench</title><h1>Owned fixture workbench</h1>')

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + token or not writes:
            self.send_error(403)
            return
        if mode == "filler" and self.path == "/api/integrations/resume-filler/application":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            application_requests.append({"method": "POST", "path": self.path, "body": body})
            self.send_json({"ok": True, "application_id": "synthetic-application"})
            return
        self.send_json({"fixture": True, "saved": True})

    def send_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


emit("verified", "resources")
if mode == "delayed-desktop":
    time.sleep(2)
for _ in range(32):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 49152 + secrets.randbelow(16384)), Handler)
        break
    except OSError:
        continue
else:
    raise SystemExit(2)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
emit("opened", "instance", instance_id=fixture_instance)
emit("ready", "runtime", instance_id=fixture_instance, api_url=f"http://127.0.0.1:{server.server_port}", writes=writes, websocket=writes)
if mode == "crash":
    raise SystemExit(2)
heartbeat_stop = threading.Event()
def heartbeat():
    while not heartbeat_stop.wait(0.2):
        emit("heartbeat", "runtime", instance_id=fixture_instance)
heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
if mode in {"desktop", "delayed-desktop", "filler"}:
    heartbeat_thread.start()
try:
    for line in sys.stdin:
        if json.loads(line).get("command") == "stop":
            break
finally:
    heartbeat_stop.set()
    if heartbeat_thread.is_alive():
        heartbeat_thread.join(timeout=2)
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    emit("stopped", "runtime", instance_id=fixture_instance)
