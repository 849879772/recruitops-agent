"""Bounded, atomic maintenance for diagnostic JSONL, never conversation storage."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Lock

TRACE_HOURS = 12
TRACE_MAX_BYTES = 32 * 1024 * 1024
_locks = defaultdict(Lock)


@contextmanager
def trace_lock(path):
    """Serialize writers/maintenance, including separate recorder instances/processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locks[str(path.resolve())], path.with_suffix(path.suffix + '.lock').open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False  # Diagnostics are best effort; never block a business task.
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prune_locked(path, *, now=None, max_bytes=TRACE_MAX_BYTES):
    """Caller holds trace_lock. Never modify the old file before replacement succeeds."""
    path = Path(path)
    if not path.is_file():
        return {'before_bytes': 0, 'after_bytes': 0}
    if path.is_symlink():
        raise ValueError('linked_trace_file')
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=TRACE_HOURS)
    size = path.stat().st_size
    temporary = None
    try:
        with path.open('rb') as source, tempfile.NamedTemporaryFile(
            mode='wb', prefix=path.name+'.prune-', dir=path.parent, delete=False,
        ) as target:
            temporary = Path(target.name)
            start = max(0, size-max_bytes)
            source.seek(start)
            if start:
                source.readline()  # Do not retain a truncated JSON record.
            for line in source:
                try:
                    row = json.loads(line)
                    when = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < cutoff:
                        continue
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                target.write(line if line.endswith(b'\n') else line+b'\n')
            target.flush()
            os.fsync(target.fileno())
        after = temporary.stat().st_size
        os.replace(temporary, path)
        temporary = None
        return {'before_bytes': size, 'after_bytes': after}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prune_trace_file(path, *, now=None):
    path = Path(path)
    if not path.is_file():
        return {'before_bytes': 0, 'after_bytes': 0}
    with trace_lock(path) as acquired:
        if not acquired:
            return {'skipped_busy': True}
        return prune_locked(path, now=now)
