"""Handle-owned Windows process trees; no PID lookup or taskkill fallback."""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
import subprocess

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
    def __init__(self):
        self.dll = kernel()
        self.children = []
        self.handle = self.dll.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeFailure("job_create_failed")
        limits = EXTENDEDLIMIT()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.dll.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.dll.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeFailure("job_configure_failed")

    def spawn(self, argv, cwd, env):
        si, pi = STARTUPINFO(), PROCESSINFO()
        si.cb = c.sizeof(si)
        command = c.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in argv]))
        block = c.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
        # Suspended creation prevents a child escaping before job assignment.
        flags = 0x4 | 0x400 | 0x08000000  # SUSPENDED | UNICODE_ENVIRONMENT | NO_WINDOW
        if not self.dll.CreateProcessW(str(argv[0]), command, None, None, False, flags,
                                      block, str(cwd), c.byref(si), c.byref(pi)):
            raise RuntimeFailure("process_spawn_failed", os_error=c.get_last_error())
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
        if failure:
            raise failure
