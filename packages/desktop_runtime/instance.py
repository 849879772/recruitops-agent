"""Persistent instance metadata and Windows current-user secret protection."""

from __future__ import annotations

import base64
import ctypes as c
from ctypes import wintypes as w
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

from . import RuntimeFailure


class Blob(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]


class TokenUser(c.Structure):
    _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]


class SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]


def protect_secret(value: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeFailure("windows_secret_protection_required")
    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [c.POINTER(Blob), c.c_void_p, c.POINTER(Blob), c.c_void_p,
                   c.c_void_p, w.DWORD, c.POINTER(Blob)]
    fn.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    buffer = c.create_string_buffer(value)
    source = Blob(len(value), c.cast(buffer, c.POINTER(c.c_ubyte)))
    output = Blob()
    # UI_FORBIDDEN; deliberately NOT LOCAL_MACHINE scope.
    if not fn(c.byref(source), None, None, None, None, 1, c.byref(output)):
        raise RuntimeFailure("credential_unprotect_failed" if decrypt else "credential_protect_failed")
    try:
        return c.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def extended_path(path: Path) -> Path:
    """Use Windows long-path IO without resolving or following reparse points."""
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def reject_links(root: Path):
    pending = [extended_path(root)]
    while pending:
        path = pending.pop()
        # Python 3.11 has no Path.is_junction; use the Windows reparse attribute.
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (info.st_file_attributes & 0x400 if os.name == "nt" else False):
            raise RuntimeFailure("linked_instance")
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())


def current_user_sid() -> str:
    if os.name != "nt":
        raise RuntimeFailure("windows_required")
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, c.byref(token)):  # TOKEN_QUERY
        raise RuntimeFailure("instance_acl_failed")
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value:
            raise RuntimeFailure("instance_acl_failed")
        buffer = c.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise RuntimeFailure("instance_acl_failed")
        user = c.cast(buffer, c.POINTER(TokenUser)).contents
        text = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(user.sid, c.byref(text)):
            raise RuntimeFailure("instance_acl_failed")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def secure_directory(root: Path, *, create=False, sid=None):
    if os.name != "nt":
        if create:
            root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        return
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    advapi.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
    advapi.SetFileSecurityW.restype = w.BOOL
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, c.POINTER(SecurityAttributes)]
    kernel.CreateDirectoryW.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    descriptor = c.c_void_p()
    sid = sid or current_user_sid()
    # Pin ownership and access to the actual token user. OWNER_RIGHTS is unsafe
    # when Windows chooses BUILTIN\Administrators as an elevated token's owner.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})", 1, c.byref(descriptor), None):
        raise RuntimeFailure("instance_acl_failed")
    try:
        if create and not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
            if not kernel.CreateDirectoryW(str(extended_path(root)), c.byref(attributes)):
                raise RuntimeFailure("instance_acl_failed")
        target = str(extended_path(root))
        # Grant the user WRITE_OWNER first. Owners implicitly have WRITE_DAC,
        # but do not implicitly have permission to set even their own SID as owner.
        if not advapi.SetFileSecurityW(target, 0x80000004, descriptor):
            raise RuntimeFailure("instance_acl_failed")
        if not advapi.SetFileSecurityW(target, 0x00000001, descriptor):
            raise RuntimeFailure("instance_acl_failed")
    finally:
        kernel.LocalFree(descriptor)


def secure_tree(root: Path):
    """One-time migration for legacy OWNER_RIGHTS instance trees."""
    sid = current_user_sid() if os.name == "nt" else None
    pending = [extended_path(root)]
    while pending:
        path = pending.pop()
        try:
            secure_directory(path, sid=sid)
            if path.is_dir():
                pending.extend(path.iterdir())
        except FileNotFoundError:
            # Temporary runtime entries may disappear after enumeration. New
            # entries inherit the already-protected parent directory ACL.
            continue


class Instance:
    def __init__(self, root: Path, *, protector=protect_secret):
        self.root, self.protector = root, protector
        self.path = root / "instance.json"
        self.record = None
        self.recovered = False

    def save(self, state, **fields):
        self.record.update(state=state, **fields)
        temporary = self.root / "instance.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def open(self, *, recover=False):
        reject_links(self.root)
        if not self.path.exists():
            if any(p.name != "runtime.lock" for p in self.root.iterdir()):
                raise RuntimeFailure("recovery_required_unknown_instance")
            password = secrets.token_hex(32)
            self.record = {"schema": 1, "instance_id": secrets.token_hex(16),
                           "postgres_major": 16, "acl_schema": 2, "root": str(self.root.resolve()),
                           "credential": base64.b64encode(self.protector(password.encode())).decode("ascii")}
            self.save("initializing")
            return password, True
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if (record.get("schema") != 1 or record.get("postgres_major") != 16
                    or record.get("root") != str(self.root.resolve())
                    or record.get("state") not in {"initializing", "starting", "migrating", "ready", "stopped", "failed", "crashed"}
                    or not re.fullmatch(r"[0-9a-f]{32}", record.get("instance_id", ""))):
                raise ValueError()
            password = self.protector(base64.b64decode(record["credential"], validate=True), decrypt=True).decode("ascii")
            if not re.fullmatch(r"[0-9a-f]{64}", password):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeFailure("invalid_instance_metadata") from exc
        if record.get("acl_schema") != 2:
            secure_tree(self.root)
            record["acl_schema"] = 2
            self.record = record
            self.save(record["state"])
        pgversion = self.root / "pgdata/PG_VERSION"
        if not pgversion.is_file() or pgversion.read_text(encoding="ascii").strip() != "16":
            backups = self.root / "backups"
            retryable_first_run = (
                record.get("state") in {"initializing", "starting", "failed"}
                and not any(backups.glob("*.dump"))
                and not (self.root / "config/runtime-capabilities.json").exists()
                and not (self.root / ".data/settings").exists()
            )
            if retryable_first_run:
                pgdata = self.root / "pgdata"
                if pgdata.exists():
                    shutil.rmtree(pgdata)
                self.record = record
                self.save("initializing")
                return password, True
            raise RuntimeFailure("recovery_required_incomplete_cluster")
        if record.get("state") != "stopped" and not recover:
            raise RuntimeFailure("recovery_required_unclean_state")
        self.record = record
        self.recovered = record.get("state") != "stopped"
        return password, False
