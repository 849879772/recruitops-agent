"""Read-only acceptance of an already-running formal Windows desktop instance.

This script never launches/stops a process, refreshes mail, or executes a task.
The API bearer is read only from the exact installed API process and never
printed or persisted. Run with Python -B to also disable bytecode writes.
"""

from __future__ import annotations

import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ENVIRONMENT_KEYS = frozenset({
    "RECRUITOPS_ENV", "RECRUITOPS_AGENT_ROOT", "RECRUITOPS_DESKTOP_INSTANCE_ID",
    "RECRUITOPS_DESKTOP_RUN_ID", "RECRUITOPS_API_PORT", "RECRUITOPS_API_TOKEN",
})
GET_ROUTES = frozenset({
    "/desktop-runtime/ready", "/desktop-runtime/activity", "/health", "/ready",
    "/api/automations", "/api/jobs?limit=1", "/api/applications/page?limit=1",
    "/api/recruitment-mails?refresh=false&limit=1", "/api/companies", "/api/schedule",
    "/api/local-ui/tasks/progress", "/api/local-ui/daily-recruitment/progress",
    "/api/codex/health", "/api/codex/threads?limit=20",
})


class VerificationError(Exception):
    """Only fixed, non-secret diagnostics are exposed to the caller."""

    def __init__(self, code, *, route=None, error_type=None):
        super().__init__(code)
        self.route = route
        self.error_type = error_type


def checked_path(path: Path) -> Path:
    resolved = path.absolute()
    for ancestor in (resolved, *resolved.parents):
        try:
            information = ancestor.lstat()
        except FileNotFoundError:
            continue
        if ancestor.is_symlink() or getattr(information, "st_file_attributes", 0) & 0x400:
            raise VerificationError("reparse_point_rejected")
    return resolved.resolve(strict=True)


class _BasicInformation(c.Structure):
    _fields_ = [("reserved1", c.c_void_p), ("peb", c.c_void_p),
                ("reserved2", c.c_void_p * 2), ("pid", c.c_void_p),
                ("reserved3", c.c_void_p)]


class _MemoryInformation(c.Structure):
    _fields_ = [("base", c.c_void_p), ("allocation_base", c.c_void_p),
                ("allocation_protect", w.DWORD), ("partition", w.WORD),
                ("region_size", c.c_size_t), ("state", w.DWORD),
                ("protect", w.DWORD), ("type", w.DWORD)]


class _TcpRow(c.Structure):
    _fields_ = [(name, w.DWORD) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "pid")]


class WindowsProcesses:
    def __init__(self):
        if os.name != "nt" or c.sizeof(c.c_void_p) != 8:
            raise VerificationError("requires_64_bit_windows_python")
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.ntdll = c.WinDLL("ntdll")
        self.iphelper = c.WinDLL("iphlpapi")
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.K32EnumProcesses.argtypes = [c.POINTER(w.DWORD), w.DWORD, c.POINTER(w.DWORD)]
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        self.kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p,
                                                  c.c_size_t, c.POINTER(c.c_size_t)]
        self.kernel.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p,
                                               c.POINTER(_MemoryInformation), c.c_size_t]
        self.kernel.VirtualQueryEx.restype = c.c_size_t
        self.ntdll.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, c.c_void_p,
                                                        w.ULONG, c.POINTER(w.ULONG)]
        self.ntdll.NtQueryInformationProcess.restype = w.LONG
        self.iphelper.GetExtendedTcpTable.argtypes = [c.c_void_p, c.POINTER(w.DWORD),
                                                       w.BOOL, w.ULONG, c.c_int, w.ULONG]

    def pids(self):
        capacity = 1024
        while capacity <= 65536:
            entries = (w.DWORD * capacity)()
            used = w.DWORD()
            if not self.kernel.K32EnumProcesses(entries, c.sizeof(entries), c.byref(used)):
                raise VerificationError("process_enumeration_failed")
            if used.value < c.sizeof(entries):
                return list(entries[:used.value // c.sizeof(w.DWORD)])
            capacity *= 2
        raise VerificationError("process_enumeration_limit")

    def listeners(self):
        size = w.DWORD()
        result = self.iphelper.GetExtendedTcpTable(None, c.byref(size), False, socket.AF_INET, 5, 0)
        if result not in (0, 122) or size.value > 8 * 1024 * 1024:
            raise VerificationError("listener_enumeration_failed")
        buffer = c.create_string_buffer(size.value)
        if self.iphelper.GetExtendedTcpTable(buffer, c.byref(size), False, socket.AF_INET, 5, 0):
            raise VerificationError("listener_enumeration_failed")
        count = w.DWORD.from_buffer(buffer).value
        if 4 + count * c.sizeof(_TcpRow) > len(buffer):
            raise VerificationError("listener_table_invalid")
        result = set()
        for index in range(count):
            row = _TcpRow.from_buffer(buffer, 4 + index * c.sizeof(_TcpRow))
            address = socket.inet_ntoa(struct.pack("<I", row.local_address))
            if row.state == 2 and address == "127.0.0.1":
                result.add((row.pid, socket.ntohs(row.local_port & 0xFFFF)))
        return result

    def _read(self, handle, address, size):
        buffer = c.create_string_buffer(size)
        used = c.c_size_t()
        if not self.kernel.ReadProcessMemory(handle, address, buffer, size, c.byref(used)):
            raise VerificationError("process_environment_unavailable")
        if used.value != size:
            raise VerificationError("process_environment_incomplete")
        return buffer.raw

    def environment(self, handle):
        information = _BasicInformation()
        used = w.ULONG()
        if self.ntdll.NtQueryInformationProcess(handle, 0, c.byref(information),
                c.sizeof(information), c.byref(used)) < 0 or not information.peb:
            raise VerificationError("process_environment_unavailable")
        # x64 Windows PEB.ProcessParameters and RTL_USER_PROCESS_PARAMETERS.Environment.
        parameters = struct.unpack("<Q", self._read(handle, information.peb + 0x20, 8))[0]
        address = struct.unpack("<Q", self._read(handle, parameters + 0x80, 8))[0]
        if not address:
            raise VerificationError("process_environment_unavailable")
        raw = bytearray()
        while len(raw) < 1024 * 1024:
            region = _MemoryInformation()
            position = address + len(raw)
            if not self.kernel.VirtualQueryEx(handle, position, c.byref(region), c.sizeof(region)):
                raise VerificationError("process_environment_unavailable")
            length = min(65536, region.base + region.region_size - position,
                         1024 * 1024 - len(raw))
            if length <= 0:
                raise VerificationError("process_environment_invalid")
            start = max(0, len(raw) - 2)
            raw.extend(self._read(handle, position, length))
            for offset in range(start, len(raw) - 3, 2):
                if raw[offset:offset + 4] == b"\0\0\0\0":
                    values = {}
                    for entry in raw[:offset].decode("utf-16-le").split("\0"):
                        key, separator, value = entry.partition("=")
                        if separator and key in ENVIRONMENT_KEYS:
                            values[key] = value
                    return values
        raise VerificationError("process_environment_limit")

    def api_environment(self, executable: Path, instance: Path, identity: dict):
        expected_executable = os.path.normcase(str(executable))
        listening = self.listeners()
        found = []
        for pid in self.pids():
            handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle:
                continue
            try:
                path = c.create_unicode_buffer(32768)
                length = w.DWORD(len(path))
                if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, c.byref(length)):
                    continue
                if os.path.normcase(path.value) != expected_executable:
                    continue
                try:
                    env = self.environment(handle)
                except VerificationError:
                    continue
                if (env.get("RECRUITOPS_ENV") != "desktop-isolated"
                        or os.path.normcase(env.get("RECRUITOPS_AGENT_ROOT", "")) != os.path.normcase(str(instance))
                        or env.get("RECRUITOPS_DESKTOP_INSTANCE_ID") != identity["instance_id"]
                        or env.get("RECRUITOPS_DESKTOP_RUN_ID") != identity["run_id"]):
                    continue
                port_text = env.get("RECRUITOPS_API_PORT", "")
                token = env.get("RECRUITOPS_API_TOKEN", "")
                if (not port_text.isdecimal() or not 49152 <= int(port_text) <= 65535
                        or not re.fullmatch(r"[0-9a-f]{64}", token)):
                    continue
                if (pid, int(port_text)) in listening:
                    found.append((pid, int(port_text), token))
            finally:
                self.kernel.CloseHandle(handle)
        if len(found) != 1:
            raise VerificationError("owned_api_listener_not_unique")
        return found[0]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise VerificationError("api_redirect_rejected")


class GetOnlyClient:
    def __init__(self, port, token):
        self.origin = f"http://127.0.0.1:{port}"
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def get(self, route, *, local_conversation=False):
        allowed_conversation = local_conversation and re.fullmatch(
            r"/api/codex/threads/[^/?]+\?include_turns=true", route)
        if route not in GET_ROUTES and not allowed_conversation:
            raise VerificationError("read_route_not_allowlisted")
        safe_route = route if route in GET_ROUTES else "/api/codex/threads/{local_only_thread_id}?include_turns=true"
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if route.startswith("/api/local-ui/"):
            headers.update({"X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "same-origin"})
        request = Request(self.origin + route, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise VerificationError("api_response_limit")
                return response.status, json.loads(raw)
        except HTTPError as error:
            return error.code, None
        except VerificationError as error:
            error.route = safe_route
            raise
        except Exception as error:
            error_type = type(error).__name__
            if isinstance(error, URLError):
                error_type += "." + type(error.reason).__name__
            raise VerificationError("api_read_failed", route=safe_route,
                                    error_type=error_type) from None


def verify(installation: Path, instance: Path, *, wait_seconds=30, require_idle=False):
    installation = checked_path(installation)
    instance = checked_path(instance)
    try:
        instance.relative_to(installation / ".data")
    except ValueError:
        raise VerificationError("instance_outside_installation_data") from None
    executable = checked_path(installation / "resources/desktop-runtime/python/python.exe")
    metadata = checked_path(instance / "instance.json")
    processes = WindowsProcesses()
    deadline = time.monotonic() + wait_seconds
    while True:
        identity = json.loads(metadata.read_text(encoding="utf-8"))
        if (identity.get("schema") != 1
                or os.path.normcase(identity.get("root", "")) != os.path.normcase(str(instance))
                or not re.fullmatch(r"[0-9a-f]{32}", identity.get("instance_id", ""))
                or not re.fullmatch(r"[0-9a-f]{32}", identity.get("run_id", ""))):
            raise VerificationError("instance_identity_invalid")
        try:
            if identity.get("state") != "ready":
                raise VerificationError("instance_not_ready")
            pid, port, token = processes.api_environment(executable, instance, identity)
            break
        except VerificationError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    client = GetOnlyClient(port, token)
    report = {"ok": False, "read_only": True, "api_pid": pid, "api_port": port,
              "instance_id": identity["instance_id"], "run_id": identity["run_id"],
              "http_statuses": {}}

    def required(route):
        status, payload = client.get(route)
        report["http_statuses"][route] = status
        if status != 200:
            raise VerificationError(f"required_get_failed:{route}:http_{status}")
        return payload

    ready = required("/desktop-runtime/ready")
    if (ready.get("status") != "ready" or ready.get("instance_id") != identity["instance_id"]
            or ready.get("run_id") != identity["run_id"]):
        raise VerificationError("api_instance_identity_mismatch")
    report["runtime"] = {key: ready.get(key) for key in ("status", "writes", "websocket")}
    health = required("/health")
    readiness = required("/ready")
    if health.get("status") != "ok" or readiness.get("status") != "ready":
        raise VerificationError("health_or_readiness_failed")
    report["health"] = {"status": health["status"], "mode": health.get("mode"),
                        "checks": readiness.get("checks")}
    activity = required("/desktop-runtime/activity")
    report["active_tasks"] = activity.get("active_tasks", [])
    if require_idle and report["active_tasks"]:
        raise VerificationError("active_business_tasks")
    automations = required("/api/automations")
    report["automations"] = {"total": automations["total"],
        "engine": {key: automations["engine"].get(key)
                   for key in ("enabled", "status", "blocked_reason")},
        "items": [{**{key: row.get(key) for key in (
            "id", "task_id", "active", "runnable", "blocked_reason", "last_status", "next_run_at")},
            "latest_execution": {key: (row.get("latest_execution") or {}).get(key)
                                 for key in ("id", "status", "thread_id", "started_at", "completed_at")}}
            for row in automations["items"]]}
    report["counts"] = {
        "jobs": required("/api/jobs?limit=1")["total"],
        "applications": required("/api/applications/page?limit=1")["total"],
        "recruitment_mails": required("/api/recruitment-mails?refresh=false&limit=1")["total"],
        "companies": len(required("/api/companies")),
        "schedule_events": len(required("/api/schedule")),
    }
    for label, route in (("tasks", "/api/local-ui/tasks/progress"),
                         ("daily", "/api/local-ui/daily-recruitment/progress")):
        progress = required(route)
        run = progress.get("run")
        report[label + "_progress"] = {"run_present": run is not None,
            "status": run.get("status") if isinstance(run, dict) else None,
            "run_count": len(progress.get("runs", []))}
    health = required("/api/codex/health")
    report["codex"] = {key: health.get(key) for key in ("enabled", "ready", "state")}
    if health.get("enabled"):
        threads = required("/api/codex/threads?limit=20")
        local = [row for row in threads.get("data", [])
                 if (row.get("automation") or {}).get("local_only") is True]
        report["conversations"] = {"page_count": len(threads.get("data", [])),
            "local_automation_count": len(local), "local_reports_checked": []}
        for row in local[:3]:
            route = "/api/codex/threads/" + quote(row["id"], safe="") + "?include_turns=true"
            status, payload = client.get(route, local_conversation=True)
            if status != 200 or not (payload.get("automation") or {}).get("local_only"):
                raise VerificationError("local_report_read_failed")
            report["conversations"]["local_reports_checked"].append({
                "id": row["id"], "http_status": status,
                "message_count": len(payload.get("messages", [])),
                "status": payload["automation"].get("status")})
    else:
        report["conversations"] = {"available": False, "reason": "codex_runtime_disabled"}
    report["ok"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--instance-root", required=True, type=Path)
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 60:
        parser.error("--wait-seconds must be between 0 and 60")
    try:
        report = verify(args.installation, args.instance_root,
                        wait_seconds=args.wait_seconds, require_idle=args.require_idle)
    except VerificationError as error:
        failure = {"ok": False, "read_only": True, "error": str(error)}
        if error.route is not None:
            failure["failed_route"] = error.route
        if error.error_type is not None:
            failure["error_type"] = error.error_type
        print(json.dumps(failure, ensure_ascii=True))
        return 2
    except Exception as error:
        # Never serialize an exception message, response body, process environment,
        # or raw traceback: any of those could include application credentials.
        print(json.dumps({"ok": False, "read_only": True, "error": "verification_failed",
                          "error_type": type(error).__name__}, ensure_ascii=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
