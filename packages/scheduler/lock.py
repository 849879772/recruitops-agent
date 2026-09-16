"""Portable process-level lock for local scheduled task invocations."""

from __future__ import annotations

import os
import threading
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockAcquisitionError(RuntimeError):
    """Raised when the context-manager form cannot acquire the lock."""


class LocalInstanceLock:
    """Hold an advisory lock on one local file until the current run finishes.

    The file is only scheduler control state. It is never a database or a
    business-data store, and it is intentionally left in place after release
    so a later invocation can reuse the same path.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._handle = None
        self._state_lock = threading.RLock()

    @property
    def is_held(self) -> bool:
        with self._state_lock:
            return self._handle is not None

    def acquire(self) -> bool:
        """Try to acquire the lock without waiting for another instance."""

        with self._state_lock:
            if self._handle is not None:
                return True

            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                handle.close()
                return False

            self._handle = handle
            return True

    def release(self) -> None:
        """Release the held lock; safe to call more than once."""

        with self._state_lock:
            handle = self._handle
            self._handle = None
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> "LocalInstanceLock":
        if not self.acquire():
            raise LockAcquisitionError(f"local scheduler lock is already held: {self.path}")
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
