"""Crawler-only resource limits shared by disposable workers on one host.

OS file locks are the leases: worker termination releases them without a stale
PID/TTL heuristic. The JSON files contain only host cooldowns, never job data.
HTTP requests and browser sessions use separate pools; Chromium's subresource
requests are not individually counted by the synchronous Playwright API.
"""

from __future__ import annotations

from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import weakref
from typing import Iterator
from urllib.parse import urlsplit

import requests


RESOURCE_ROOT_ENV = "RECRUITOPS_CRAWL_RESOURCE_ROOT"
BROWSER_LIMIT_ENV = "RECRUITOPS_BROWSER_MAX_CONCURRENCY"
RESOURCE_DEADLINE_ENV = "RECRUITOPS_CRAWL_RESOURCE_DEADLINE"
SITE_MAX_CONCURRENCY = 2
RESOURCE_WAIT_ALLOWANCE_SECONDS = 60.0
_wait_lock = threading.Lock()
_wait_seconds = {"browser": 0.0, "http": 0.0}
_wait_count = {"browser": 0, "http": 0}


class ResourceLimitTimeout(requests.Timeout):
    """Resource admission exhausted its bounded wait without a network call."""


def resource_root() -> Path:
    configured = os.environ.get(RESOURCE_ROOT_ENV)
    if configured:
        return Path(configured).resolve()
    root = Path(os.environ.get("RECRUITOPS_AGENT_ROOT") or Path(__file__).resolve().parents[2])
    return (root / ".data" / "crawl-resources").resolve()


def admission_timeout() -> float:
    remaining = (float(os.environ[RESOURCE_DEADLINE_ENV]) - time.monotonic()
                 if RESOURCE_DEADLINE_ENV in os.environ else RESOURCE_WAIT_ALLOWANCE_SECONDS)
    with _wait_lock:
        budget = RESOURCE_WAIT_ALLOWANCE_SECONDS - sum(_wait_seconds.values())
    return max(0.0, min(remaining, budget))


def _record_wait(kind: str, started: float) -> None:
    elapsed = max(0.0, time.monotonic() - started)
    with _wait_lock:
        _wait_seconds[kind] += elapsed
        _wait_count[kind] += 1


def resource_timing_snapshot() -> dict[str, float | int]:
    """Bounded aggregate diagnostics; no sites or candidate data are included."""
    with _wait_lock:
        return {
            "browser_wait_seconds": round(_wait_seconds["browser"], 3),
            "http_wait_seconds": round(_wait_seconds["http"], 3),
            "browser_acquisitions": _wait_count["browser"],
            "http_acquisitions": _wait_count["http"],
        }


def reset_resource_timings() -> None:
    """Start a fresh worker operation without carrying prior test or job waits."""
    with _wait_lock:
        for kind in ("browser", "http"):
            _wait_seconds[kind] = 0.0
            _wait_count[kind] = 0


class FileLease:
    """An idempotently releasable, non-inheritable OS lock handle."""

    def __init__(self, handle):
        self.handle = handle
        self._guard = threading.Lock()

    def release(self) -> None:
        with self._guard:
            if self.handle is not None:
                # Closing also releases the byte lock on Windows and flock on Unix.
                self.handle.close()
                self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def _try_lease(path: Path) -> FileLease | None:
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return FileLease(handle)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return None
        raise
    except BaseException:
        handle.close()
        raise


class ResourcePool:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else resource_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def acquire(self, name: str, limit: int, *, timeout: float | None = None,
                cancel: threading.Event | None = None) -> FileLease:
        if limit < 1:
            raise ValueError("resource limit must be positive")
        deadline = time.monotonic() + (admission_timeout() if timeout is None else max(0.0, timeout))
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()
        while True:
            if cancel is not None and cancel.is_set():
                raise ResourceLimitTimeout("crawler resource admission cancelled")
            for slot in range(limit):
                lease = _try_lease(self.root / f"{key}.{slot}.lock")
                if lease is not None:
                    return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResourceLimitTimeout(f"crawler resource admission timed out: {name}")
            if cancel is not None:
                cancel.wait(min(0.025, remaining))
            else:
                time.sleep(min(0.025, remaining))

    @contextmanager
    def _state(self, host: str, *, timeout: float | None = None):
        key = hashlib.sha256(host.encode("utf-8")).hexdigest()
        with self.acquire("cooldown:" + host, 1, timeout=timeout):
            path = self.root / f"{key}.cooldown.json"
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                state = {"failures": 0, "until": 0.0}
            yield state
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(path)

    def acquire_http(self, url: str, *, timeout: float | None = None) -> FileLease:
        host = urlsplit(url).hostname or "unknown"
        budget = admission_timeout() if timeout is None else max(0.0, timeout)
        deadline = time.monotonic() + budget
        while True:
            with self._state(host, timeout=max(0.0, deadline - time.monotonic())) as state:
                delay = float(state.get("until", 0)) - time.time()
            if delay > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResourceLimitTimeout(f"crawler host cooldown exceeds admission budget: {host}")
                time.sleep(min(delay, 0.1, remaining))
                continue
            lease = self.acquire("http:" + host, SITE_MAX_CONCURRENCY,
                                 timeout=max(0.0, deadline - time.monotonic()))
            # A concurrent failure may have started cooldown while we waited.
            try:
                with self._state(host, timeout=max(0.0, deadline - time.monotonic())) as state:
                    cooling = float(state.get("until", 0)) > time.time()
            except BaseException:
                lease.release()
                raise
            if not cooling:
                return lease
            lease.release()

    def record_http(self, url: str, *, status: int | None = None,
                    retry_after: str | None = None, failed: bool = False) -> None:
        host = urlsplit(url).hostname or "unknown"
        failed = failed or status in {408, 429, 502, 503, 504}
        with self._state(host) as state:
            if failed:
                count = min(int(state.get("failures", 0)) + 1, 10)
                delay = min(30.0, 0.5 * 2 ** (count - 1))
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            pass
                state.update(failures=count, until=max(float(state.get("until", 0)), time.time() + delay))
            else:
                # A successful concurrent request cannot cancel an active 429 delay.
                state["failures"] = 0


@contextmanager
def worker_http_limits() -> Iterator[None]:
    """Patch only inside a disposable crawler worker, never in the API process.

    The adapter boundary also covers Sessions and adapter-owned thread pools.
    Redirects release the prior host before acquiring their next host, so there
    is no nested host lease. Streaming responses retain a lease until close.
    """
    original = requests.adapters.HTTPAdapter.send
    pool = ResourcePool()

    def send(adapter, request, **kwargs):
        wait_started = time.monotonic()
        try:
            lease = pool.acquire_http(request.url)
        finally:
            _record_wait("http", wait_started)
        response = None
        try:
            response = original(adapter, request, **kwargs)
            if not kwargs.get("stream", False):
                _ = response.content
            pool.record_http(request.url, status=response.status_code,
                             retry_after=response.headers.get("Retry-After"))
            if kwargs.get("stream", False):
                close = response.close

                def close_response():
                    try:
                        return close()
                    finally:
                        lease.release()

                response.close = close_response
                weakref.finalize(response, lease.release)
                return response
            lease.release()
            return response
        except BaseException as exc:
            lease.release()
            if response is not None:
                response.close()
            if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                pool.record_http(request.url, failed=True)
            raise

    requests.adapters.HTTPAdapter.send = send
    try:
        yield
    finally:
        requests.adapters.HTTPAdapter.send = original


def launch_limited_browser(playwright, **kwargs):
    """Reserve a browser slot until close/disconnect, including across workers."""
    limit = max(1, min(6, int(os.environ.get(BROWSER_LIMIT_ENV, "6"))))
    wait_started = time.monotonic()
    try:
        lease = ResourcePool().acquire("browser", limit)
    finally:
        _record_wait("browser", wait_started)
    browser = None
    try:
        browser = playwright.chromium.launch(**kwargs)
        close = browser.close

        def close_browser(*args, **close_kwargs):
            try:
                return close(*args, **close_kwargs)
            finally:
                lease.release()

        browser.close = close_browser
        if callable(getattr(browser, "on", None)):
            browser.on("disconnected", lambda *_args: lease.release())
        try:
            weakref.finalize(browser, lease.release)
        except TypeError:
            pass  # Some deterministic browser fixtures do not support weakrefs.
        return browser
    except BaseException:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        lease.release()
        raise
