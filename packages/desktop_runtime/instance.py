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


def secure_directory(root: Path):
    if os.name != "nt":
        root.chmod(0o700)
        return
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    advapi.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
    advapi.SetFileSecurityW.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    descriptor = c.c_void_p()
    # Protected DACL, inheritable owner-rights + SYSTEM only. No Everyone/Users.
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)", 1, c.byref(descriptor), None):
        raise RuntimeFailure("instance_acl_failed")
    try:
        if not advapi.SetFileSecurityW(str(root), 0x80000004, descriptor):
            raise RuntimeFailure("instance_acl_failed")
    finally:
        kernel.LocalFree(descriptor)


class Instance:
    def __init__(self, root: Path, *, protector=protect_secret):
        self.root, self.protector = root, protector
        self.path = root / "instance.json"
        self.record = None

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
            secure_directory(self.root)
            password = secrets.token_hex(32)
            self.record = {"schema": 1, "instance_id": secrets.token_hex(16),
                           "postgres_major": 16, "root": str(self.root.resolve()),
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
        return password, False
