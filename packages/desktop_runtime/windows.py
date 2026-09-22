"""Handle-owned Windows process trees; no PID lookup or taskkill fallback."""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
import subprocess

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - WindowsTree is rejected before use on other platforms.
    msvcrt = None

from . import RuntimeFailure


class STARTUPINFO(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), ("x", w.DWORD), ("y", w.DWORD),
                ("cx", w.DWORD), ("cy", w.DWORD), ("charsx", w.DWORD),
                ("charsy", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved2size", w.WORD), ("reserved2", c.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class PROCESSINFO(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class BASICLIMIT(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                ("flags", w.DWORD), ("minimum", c.c_size_t), ("maximum", c.c_size_t),
                ("active", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class EXTENDEDLIMIT(c.Structure):
    _fields_ = [("basic", BASICLIMIT), ("io", c.c_uint64 * 6),
                ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


def kernel():
    if os.name != "nt":
        raise RuntimeFailure("windows_required")
    dll = c.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                            c.c_void_p, w.LPCWSTR, c.POINTER(STARTUPINFO), c.POINTER(PROCESSINFO)], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
    return dll


def security():
    dll = c.WinDLL("advapi32", use_last_error=True)
    signatures = {
        "CreateWellKnownSid": ([c.c_int, c.c_void_p, c.c_void_p, c.POINTER(w.DWORD)], w.BOOL),
        "CheckTokenMembership": ([w.HANDLE, c.c_void_p, c.POINTER(w.BOOL)], w.BOOL),
        "OpenProcessToken": ([w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL),
        "CreateRestrictedToken": ([w.HANDLE, w.DWORD, w.DWORD, c.c_void_p, w.DWORD,
                                   c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(w.HANDLE)], w.BOOL),
        "CreateProcessAsUserW": ([w.HANDLE, w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p,
                                  w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR,
                                  c.POINTER(STARTUPINFO), c.POINTER(PROCESSINFO)], w.BOOL),
        "CreateProcessWithTokenW": ([w.HANDLE, w.DWORD, w.LPCWSTR, w.LPWSTR, w.DWORD,
                                     c.c_void_p, w.LPCWSTR, c.POINTER(STARTUPINFO),
                                     c.POINTER(PROCESSINFO)], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
    return dll


def create_restricted_token(kernel_dll, security_dll):
    source, restricted = w.HANDLE(), w.HANDLE()
    # TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY
    if not security_dll.OpenProcessToken(kernel_dll.GetCurrentProcess(), 0x000B, c.byref(source)):
        raise RuntimeFailure("process_token_failed", os_error=c.get_last_error())
    try:
        # DISABLE_MAX_PRIVILEGE | LUA_TOKEN. The user SID and DPAPI identity are
        # preserved while enabled administrative membership is removed.
        if not security_dll.CreateRestrictedToken(source, 0x0005, 0, None, 0, None, 0, None,
                                                  c.byref(restricted)):
            raise RuntimeFailure("process_token_failed", os_error=c.get_last_error())
        return restricted
    finally:
        kernel_dll.CloseHandle(source)


def elevated_restricted_token(kernel_dll, security_dll):
    """Return a same-user LUA token only when the current token is elevated."""
    sid = c.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
    size = w.DWORD(len(sid))
    if not security_dll.CreateWellKnownSid(26, None, sid, c.byref(size)):  # WinBuiltinAdministratorsSid
        raise RuntimeFailure("process_token_failed", os_error=c.get_last_error())
    member = w.BOOL()
    if not security_dll.CheckTokenMembership(None, sid, c.byref(member)):
        raise RuntimeFailure("process_token_failed", os_error=c.get_last_error())
    return create_restricted_token(kernel_dll, security_dll) if member.value else None


class OwnedProcess:
    def __init__(self, dll, handle, pid):
        self.dll, self.handle, self.pid = dll, handle, pid

    def poll(self):
        if self.dll.WaitForSingleObject(self.handle, 0) == 258:
            return None
        code = w.DWORD()
        if not self.dll.GetExitCodeProcess(self.handle, c.byref(code)):
            raise RuntimeFailure("process_status_failed")
        return code.value

    def wait(self, timeout=30):
        result = self.dll.WaitForSingleObject(self.handle, int(timeout * 1000))
        if result == 258:
            raise RuntimeFailure("process_timeout")
        if result != 0:
            raise RuntimeFailure("process_wait_failed")
        return self.poll()

    def close(self):
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None

    def terminate(self):
        if self.poll() is None and not self.dll.TerminateProcess(self.handle, 1):
            raise RuntimeFailure("process_terminate_failed")


class WindowsTree:
    def __init__(self, *, token_factory=elevated_restricted_token):
        self.dll = kernel()
        self.security = security()
        self.children = []
        self.restricted_token = token_factory(self.dll, self.security)
        self.handle = self.dll.CreateJobObjectW(None, None)
        if not self.handle:
            if self.restricted_token:
                self.dll.CloseHandle(self.restricted_token)
                self.restricted_token = None
            raise RuntimeFailure("job_create_failed")
        limits = EXTENDEDLIMIT()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.dll.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.dll.CloseHandle(self.handle)
            self.handle = None
            if self.restricted_token:
                self.dll.CloseHandle(self.restricted_token)
                self.restricted_token = None
            raise RuntimeFailure("job_configure_failed")

    def spawn(self, argv, cwd, env, *, output=None):
        si, pi = STARTUPINFO(), PROCESSINFO()
        si.cb = c.sizeof(si)
        command_text = subprocess.list2cmdline([str(a) for a in argv])
        command = c.create_unicode_buffer(command_text)
        block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
        streams = []
        inherit = False
        if output is not None:
            output = os.fspath(output)
            os.makedirs(os.path.dirname(output), exist_ok=True)
            stdin = open(os.devnull, "rb", buffering=0)
            log = open(output, "wb", buffering=0)
            streams.extend((stdin, log))
            stdin_handle = msvcrt.get_osfhandle(stdin.fileno())
            log_handle = msvcrt.get_osfhandle(log.fileno())
            os.set_handle_inheritable(stdin_handle, True)
            os.set_handle_inheritable(log_handle, True)
            si.flags |= 0x100  # STARTF_USESTDHANDLES
            si.stdin, si.stdout, si.stderr = stdin_handle, log_handle, log_handle
            inherit = True
        # Suspended creation prevents a child escaping before job assignment.
        flags = 0x4 | 0x400 | 0x08000000  # SUSPENDED | UNICODE_ENVIRONMENT | NO_WINDOW
        try:
            if self.restricted_token:
                created = self.security.CreateProcessAsUserW(
                    self.restricted_token, str(argv[0]), command, None, None, inherit, flags,
                    block, str(cwd), c.byref(si), c.byref(pi))
                if not created and c.get_last_error() == 1314:  # ERROR_PRIVILEGE_NOT_HELD
                    command = c.create_unicode_buffer(command_text)
                    created = self.security.CreateProcessWithTokenW(
                        self.restricted_token, 0, str(argv[0]), command, flags,
                        block, str(cwd), c.byref(si), c.byref(pi))
            else:
                created = self.dll.CreateProcessW(str(argv[0]), command, None, None, inherit, flags,
                                                  block, str(cwd), c.byref(si), c.byref(pi))
            if not created:
                raise RuntimeFailure("process_spawn_failed", os_error=c.get_last_error())
        finally:
            for stream in streams:
                stream.close()
        try:
            if not self.dll.AssignProcessToJobObject(self.handle, pi.process):
                raise RuntimeFailure("job_assign_failed", os_error=c.get_last_error())
            if self.dll.ResumeThread(pi.thread) == 0xFFFFFFFF:
                raise RuntimeFailure("process_resume_failed")
        except BaseException:
            self.dll.TerminateProcess(pi.process, 1)
            self.dll.WaitForSingleObject(pi.process, 5000)
            self.dll.CloseHandle(pi.process)
            raise
        finally:
            self.dll.CloseHandle(pi.thread)
        process = OwnedProcess(self.dll, pi.process, pi.pid)
        self.children.append(process)
        return process

    def close(self):
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None
        failure = None
        for process in self.children:
            try:
                process.wait(5)
            except RuntimeFailure as exc:
                failure = exc
            finally:
                process.close()
        self.children.clear()
        if self.restricted_token:
            self.dll.CloseHandle(self.restricted_token)
            self.restricted_token = None
        if failure:
            raise failure
