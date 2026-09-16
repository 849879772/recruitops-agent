"""Run-scoped reuse of verified official job-detail API responses.

The wrapper is intentionally conservative.  It only creates a reuse key for
the controlled ByteDance and Feishu detail routes, and it only caches a plain
``dict`` after the response proves the requested post ID and exact title and
has a complete, terminated, hash-matching official-API capture receipt.

``ReusingDetailHydrator`` owns all state.  Create one instance for one run and
call ``close`` when that run ends.  The instance is thread-safe, uses a
bounded LRU cache measured in bytes as well as entries, and performs a
singleflight leader fetch in the caller's thread.  Failed or unverified
responses are shared only with already-waiting callers and are never cached.

The fetch callable is called as ``fetch(job, timeout_seconds=seconds)``.  A
mapping result is copied and receives a wrapper-owned ``_detail_reuse`` field
with ``request_made`` and ``reused`` booleans.  Non-mapping legacy results are
returned unchanged because they cannot carry the metadata contract safely.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import math
import re
import sys
from threading import Event, RLock
from time import monotonic
import unicodedata
from typing import Any
from urllib.parse import unquote, urlsplit

from packages.domain.job_identity import normalize_job_identity_url


DEFAULT_MAX_ENTRIES = 4096
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 45.0

_SUPPORTED_FEISHU_SUFFIX = ".jobs.feishu.cn"
_TENANT_FIELDS = (
    "tenant_id",
    "tenantId",
    "source_tenant_id",
    "sourceTenantId",
    "tenant",
    "source_tenant",
)
_EXPLICIT_POST_ID_FIELDS = (
    "native_job_id",
    "nativeID",
    "nativeId",
    "native_id",
    "source_job_id",
    "sourceJobId",
    "source_post_id",
    "sourcePostId",
    "post_id",
    "postId",
    "position_id",
    "positionId",
)
_COMPANY_EXACT_FIELDS = {
    "company",
    "company_id",
    "company_name",
    "employer",
    "employer_id",
    "employer_name",
    "organization",
    "organization_id",
    "organization_name",
    "source_company",
    "source_company_id",
    "source_company_name",
}
_HASH_FIELDS = ("content_sha256", "capture_hash", "sha256")


@dataclass(frozen=True, slots=True)
class DetailReuseKey:
    """The complete identity used for one reusable official detail request."""

    platform: str
    tenant: str
    post_id: str
    detail_url: str
    title: str

    @property
    def token(self) -> str:
        payload = "\0".join(
            (self.platform, self.tenant, self.post_id, self.detail_url, self.title)
        )
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Route:
    platform: str
    host: str
    prefix: tuple[str, ...]
    post_id: str

    @property
    def tenant_scope(self) -> str:
        suffix = "/".join(self.prefix)
        return f"{self.host}/{suffix}" if suffix else self.host


@dataclass(frozen=True, slots=True)
class _KeyContext:
    key: DetailReuseKey
    company_scope: str | None
    tenant_hint: str | None


@dataclass(slots=True)
class _CacheEntry:
    value: dict[str, Any]
    size: int


@dataclass(slots=True)
class _InFlight:
    event: Event
    deadline: float
    company_scope: str | None
    result: Any = None
    error: Exception | None = None
    result_is_dict: bool = False
    cacheable: bool = False
    company_bound: bool = False
    reason: str = ""


def _build_key_context(job: object) -> tuple[_KeyContext | None, str]:
    if not isinstance(job, Mapping):
        return None, "job_not_mapping"
    url = _normalised_url(job)
    if not url:
        return None, "detail_url_not_canonical"
    route = _official_route(url)
    if route is None:
        return None, "unsupported_official_detail_route"
    if _explicit_post_id_conflicts(job, route):
        return None, "explicit_post_id_conflict"
    title = _canonical_title(job.get("title"))
    if not title:
        return None, "title_missing"
    tenant_hint, conflict = _tenant_hint(job)
    if conflict:
        return None, "tenant_binding_conflict"
    tenant = route.tenant_scope + (f"|{tenant_hint}" if tenant_hint else "")
    key = DetailReuseKey(route.platform, tenant, route.post_id, url, title)
    return _KeyContext(key, _source_company_scope(job), tenant_hint), ""


def _text(value: object) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return ""
    return str(value).strip()


def _canonical_title(value: object) -> str:
    return unicodedata.normalize("NFKC", _text(value))


def _canonical_identity(value: object) -> str:
    return unicodedata.normalize("NFKC", _text(value)).casefold()


def _canonical_post_id(value: object) -> str:
    text = _text(value)
    if not text or not text.isdigit():
        return text
    return str(int(text))


def _explicit_post_id_conflicts(job: Mapping[str, Any], route: _Route) -> bool:
    """Reject explicit native IDs that disagree with the controlled URL route."""

    return any(
        _canonical_post_id(job.get(field)) != route.post_id
        for field in _EXPLICIT_POST_ID_FIELDS
        if _text(job.get(field))
    )


def _normalised_url(job: Mapping[str, Any]) -> str:
    values: list[str] = []
    for field in ("detail_url", "jd_url"):
        raw = _text(job.get(field))
        if not raw:
            continue
        normalised = _normalised_official_url(raw)
        if not normalised:
            return ""
        values.append(normalised)
    if not values or len(set(values)) != 1:
        return ""
    return values[0]


def _normalised_official_url(raw: object) -> str:
    """Validate the original URL before normalization can discard security signals."""

    text = _text(raw)
    if not text or _official_route(text) is None:
        return ""
    return normalize_job_identity_url(text)


def _official_route(url: str) -> _Route | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or (port not in (None, 443))
    ):
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        return None

    raw_segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        segments = [unquote(segment) for segment in raw_segments]
    except Exception:
        return None
    if len(segments) < 3:
        return None
    if (
        segments[-3].casefold() != "position"
        or not re.fullmatch(r"[0-9]+", segments[-2])
        or segments[-1].casefold() != "detail"
    ):
        return None
    prefix = tuple(segments[:-3])
    if any(not segment or "/" in segment for segment in prefix):
        return None

    if host == "jobs.bytedance.com":
        if prefix not in ((), ("campus",)):
            return None
        platform = "bytedance"
    elif host.endswith(_SUPPORTED_FEISHU_SUFFIX) and host != "jobs.feishu.cn":
        platform = "feishu"
    else:
        return None
    return _Route(platform, host, prefix, _canonical_post_id(segments[-2]))


def _tenant_hint(job: Mapping[str, Any]) -> tuple[str | None, bool]:
    values = {
        _canonical_identity(job.get(field))
        for field in _TENANT_FIELDS
        if _text(job.get(field))
    }
    if len(values) > 1:
        return None, True
    return next(iter(values), None), False


def _normalise_field_name(value: object) -> str:
    return _text(value).casefold().replace("-", "_")


def _is_company_field(value: object) -> bool:
    name = _normalise_field_name(value)
    return name in _COMPANY_EXACT_FIELDS or any(
        marker in name for marker in ("company", "employer", "organization")
    )


def _source_company_scope(job: Mapping[str, Any]) -> str | None:
    values = {
        _canonical_identity(value)
        for key, value in job.items()
        if _is_company_field(key) and _text(value)
    }
    if not values:
        return None
    return "\x1f".join(sorted(values))


def _has_company_binding(value: object, *, _nested: bool = False) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_company_field(key) and _text(item):
                return True
            if _nested and _has_company_binding(item, _nested=True):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str) and ":" in item:
                prefix, _, item_value = item.partition(":")
                if _is_company_field(prefix) and _text(item_value):
                    return True
            elif _nested and _has_company_binding(item, _nested=True):
                return True
    return False


def _evidence_values(value: object, prefix: str) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    expected = _normalise_field_name(prefix)
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or ":" not in item:
            continue
        item_prefix, _, item_value = item.partition(":")
        if _normalise_field_name(item_prefix) == expected and _text(item_value):
            values.append(_text(item_value))
    return values


def _native_values_match(values: list[str], expected: str) -> bool:
    return len(values) == 1 and _canonical_post_id(values[0]) == _canonical_post_id(expected)


def _sha256_detail(detail: object) -> str:
    text = _text(detail)
    return sha256(text.encode("utf-8")).hexdigest() if text else ""


def _tenant_values(result: Mapping[str, Any]) -> list[str]:
    values = [
        _text(result.get(field))
        for field in _TENANT_FIELDS
        if _text(result.get(field))
    ]
    evidence = result.get("identity_evidence")
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if not isinstance(item, str) or ":" not in item:
                continue
            prefix, _, item_value = item.partition(":")
            if _normalise_field_name(prefix) in {
                "tenant",
                "tenant_id",
                "sourcetenantid",
                "source_tenant_id",
            } and _text(item_value):
                values.append(_text(item_value))
    return values


def _deep_size(value: object) -> int:
    """Approximate retained Python memory for a cache value and its children."""

    seen: set[int] = set()
    pending = [value]
    total = 0
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(current, 0)
        if isinstance(current, Mapping):
            for key, item in current.items():
                pending.extend((key, item))
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending.extend(current)
        elif hasattr(current, "__dict__"):
            pending.append(vars(current))
    return total


def _copy_job(job: object) -> object:
    try:
        return deepcopy(job)
    except Exception:
        return job


class ReusingDetailHydrator:
    """Thread-safe, run-scoped singleflight and bounded detail-response cache."""

    def __init__(
        self,
        fetch: Callable[..., Any],
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_inflight: int | None = None,
        default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(fetch):
            raise TypeError("fetch must be callable")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if max_inflight is None:
            max_inflight = max_entries
        if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight <= 0:
            raise ValueError("max_inflight must be a positive integer")
        self._fetch = fetch
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_inflight = max_inflight
        self.default_timeout_seconds = self._validate_timeout(default_timeout_seconds)
        self._lock = RLock()
        self._cache: OrderedDict[DetailReuseKey, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[DetailReuseKey, _InFlight] = {}
        self._closed = False
        self._metrics: dict[str, int] = {
            "requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_admissions": 0,
            "cache_rejections": 0,
            "cache_evictions": 0,
            "fetch_calls": 0,
            "fetch_complete": 0,
            "fetch_exceptions": 0,
            "singleflight_leaders": 0,
            "singleflight_waits": 0,
            "singleflight_shared_successes": 0,
            "singleflight_shared_failures": 0,
            "singleflight_unverified_waits": 0,
            "singleflight_exception_waits": 0,
            "singleflight_wait_timeouts": 0,
            "company_bound_blocks": 0,
            "direct_calls": 0,
            "unkeyable_calls": 0,
            "inflight_capacity_bypasses": 0,
        }

    @staticmethod
    def _validate_timeout(value: object) -> float:
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be positive")
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        return timeout

    def __enter__(self) -> "ReusingDetailHydrator":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        """End the run and release cached values; in-flight fetches are not cancelled."""

        with self._lock:
            self._closed = True
            self._cache.clear()
            self._cache_bytes = 0

    def snapshot(self) -> dict[str, Any]:
        """Return bounded-cache and singleflight counters for run diagnostics."""

        with self._lock:
            return {
                **self._metrics,
                "cache_entries": len(self._cache),
                "cache_bytes": self._cache_bytes,
                "inflight": len(self._inflight),
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "max_inflight": self.max_inflight,
                "closed": self._closed,
            }

    metrics_snapshot = snapshot

    def hydrate(self, job: object, *, timeout_seconds: float | None = None) -> Any:
        """Fetch one job, reusing only a verified official API response."""

        timeout = self.default_timeout_seconds if timeout_seconds is None else self._validate_timeout(timeout_seconds)
        started = monotonic()
        with self._lock:
            self._ensure_open()
            self._metrics["requests"] += 1

        context, key_reason = _build_key_context(job)
        if context is None:
            with self._lock:
                self._metrics["unkeyable_calls"] += 1
                self._metrics["direct_calls"] += 1
            result = self._invoke_fetch(job, timeout)
            return self._direct_response(result, key_reason)

        with self._lock:
            cached = self._cache.get(context.key)
            if cached is not None:
                self._cache.move_to_end(context.key)
                self._metrics["cache_hits"] += 1
                cached_value = cached.value
            else:
                self._metrics["cache_misses"] += 1
                cached_value = None
            if cached_value is not None:
                pass
            else:
                flight = self._inflight.get(context.key)
                if flight is not None:
                    self._metrics["singleflight_waits"] += 1
                    leader = False
                elif len(self._inflight) >= self.max_inflight:
                    self._metrics["inflight_capacity_bypasses"] += 1
                    flight = None
                    leader = False
                else:
                    flight = _InFlight(
                        event=Event(),
                        deadline=started + timeout,
                        company_scope=context.company_scope,
                    )
                    self._inflight[context.key] = flight
                    self._metrics["singleflight_leaders"] += 1
                    leader = True

        if cached_value is not None:
            return self._decorate(
                cached_value,
                context.key,
                reused=True,
                mode="cache",
                request_executed=False,
                cacheable=True,
                reason="cache_hit",
            )
        if flight is not None and not leader:
            return self._wait_for_flight(context, flight, timeout)
        if flight is None:
            with self._lock:
                self._metrics["direct_calls"] += 1
            result = self._invoke_fetch(job, timeout)
            return self._direct_response(result, "inflight_capacity")

        try:
            result = self._invoke_fetch(job, timeout)
        except Exception as exc:
            with self._lock:
                flight.error = exc
                self._finish_flight(context.key, flight)
            raise

        raw_result, is_dict = self._raw_result(result)
        cacheable = False
        company_bound = False
        reason = "unsupported_result"
        if is_dict:
            cacheable, reason, company_bound = self._validate_response(context, raw_result)
            with self._lock:
                self._metrics["fetch_complete"] += int(str(raw_result.get("status") or "").casefold() == "complete")
                if cacheable and not self._closed:
                    admitted, admission_reason = self._admit_cache(context.key, raw_result)
                    if admitted:
                        self._metrics["cache_admissions"] += 1
                    else:
                        cacheable = False
                        reason = admission_reason
                elif cacheable:
                    cacheable = False
                    reason = "hydrator_closed"
                if not cacheable:
                    self._metrics["cache_rejections"] += 1
        else:
            with self._lock:
                self._metrics["cache_rejections"] += 1

        with self._lock:
            flight.result = raw_result
            flight.result_is_dict = is_dict
            flight.cacheable = cacheable
            flight.company_bound = company_bound
            flight.reason = reason
            self._finish_flight(context.key, flight)
        return self._decorate(
            raw_result,
            context.key,
            reused=False,
            mode="leader",
            request_executed=True,
            cacheable=cacheable,
            reason=reason,
        )

    __call__ = hydrate

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("detail reuse hydrator is closed")

    def _invoke_fetch(self, job: object, timeout: float) -> Any:
        with self._lock:
            self._metrics["fetch_calls"] += 1
        try:
            return self._fetch(_copy_job(job), timeout_seconds=timeout)
        except Exception:
            with self._lock:
                self._metrics["fetch_exceptions"] += 1
            raise

    @staticmethod
    def _raw_result(result: Any) -> tuple[Any, bool]:
        if not isinstance(result, dict):
            try:
                return deepcopy(result), False
            except Exception:
                return result, False
        raw = deepcopy(dict(result))
        raw.pop("_detail_reuse", None)
        return raw, True

    @staticmethod
    def _validate_response(
        context: _KeyContext,
        result: dict[str, Any],
    ) -> tuple[bool, str, bool]:
        company_bound = _has_company_binding(result, _nested=True)
        if company_bound:
            return False, "company_bound_response", True
        if str(result.get("status") or "").casefold() != "complete":
            return False, "status_not_complete", False
        if str(result.get("identity_status") or "").casefold() not in {"matched", "request_bound"}:
            return False, "identity_status_not_verified", False

        native_values = _evidence_values(result.get("identity_evidence"), "native_id")
        title_values = _evidence_values(result.get("identity_evidence"), "title")
        if native_values is None or title_values is None:
            return False, "identity_evidence_missing", False
        if not _native_values_match(native_values, context.key.post_id):
            return False, "native_id_evidence_mismatch", False
        if len(title_values) != 1 or _canonical_title(title_values[0]) != context.key.title:
            return False, "title_evidence_mismatch", False

        capture = result.get("capture_evidence")
        if not isinstance(capture, Mapping):
            return False, "capture_evidence_missing", False
        required = (
            "status",
            "method",
            "source_url",
            "identity_verified",
            "terminal_observed",
            "remaining_controls",
        )
        if any(field not in capture for field in required):
            return False, "capture_evidence_incomplete", False
        if str(capture.get("status") or "").casefold() != "complete":
            return False, "capture_not_complete", False
        if str(capture.get("method") or "").casefold() != "official_api":
            return False, "capture_method_not_official_api", False
        if capture.get("identity_verified") is not True:
            return False, "capture_identity_not_verified", False
        if capture.get("terminal_observed") is not True or capture.get("remaining_controls"):
            return False, "capture_not_terminal", False
        capture_source_url = _text(capture.get("source_url"))
        capture_url = _normalised_official_url(capture_source_url)
        if not capture_url:
            return False, "capture_source_url_untrusted", False
        if capture_url != context.key.detail_url:
            return False, "capture_source_url_mismatch", False
        result_source_url = _text(result.get("detail_url"))
        result_url = _normalised_official_url(result_source_url) if result_source_url else ""
        if result_source_url and not result_url:
            return False, "result_detail_url_untrusted", False
        if result_url and result_url != context.key.detail_url:
            return False, "result_detail_url_mismatch", False

        expected_hash = _sha256_detail(result.get("detail"))
        if not expected_hash:
            return False, "detail_missing", False
        hashes = [_text(capture.get(field)) for field in _HASH_FIELDS if _text(capture.get(field))]
        if len(hashes) != 1 or hashes[0].casefold() != expected_hash:
            return False, "capture_hash_mismatch", False

        if context.tenant_hint:
            observed_tenants = _tenant_values(result)
            if observed_tenants and any(
                _canonical_identity(value) != context.tenant_hint for value in observed_tenants
            ):
                return False, "tenant_evidence_mismatch", False
        return True, "verified_official_api", False

    def _admit_cache(self, key: DetailReuseKey, value: dict[str, Any]) -> tuple[bool, str]:
        size = _deep_size((key.platform, key.tenant, key.post_id, key.detail_url, key.title, value))
        if size > self.max_bytes:
            return False, "cache_entry_too_large"
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous.size
        while self._cache and (
            len(self._cache) >= self.max_entries or self._cache_bytes + size > self.max_bytes
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.size
            self._metrics["cache_evictions"] += 1
        self._cache[key] = _CacheEntry(deepcopy(value), size)
        self._cache_bytes += size
        return True, "verified_official_api"

    def _finish_flight(self, key: DetailReuseKey, flight: _InFlight) -> None:
        if self._inflight.get(key) is flight:
            self._inflight.pop(key, None)
        flight.event.set()

    def _wait_for_flight(self, context: _KeyContext, flight: _InFlight, timeout: float) -> Any:
        remaining = min(timeout, max(0.0, flight.deadline - monotonic()))
        if remaining <= 0 or not flight.event.wait(remaining):
            with self._lock:
                self._metrics["singleflight_wait_timeouts"] += 1
            return self._timeout_response(context.key)
        if flight.error is not None:
            with self._lock:
                self._metrics["singleflight_shared_failures"] += 1
                self._metrics["singleflight_exception_waits"] += 1
            return self._exception_response(context.key, flight.error)
        if flight.company_bound and not (
            flight.company_scope is not None
            and context.company_scope == flight.company_scope
        ):
            with self._lock:
                self._metrics["company_bound_blocks"] += 1
            return self._company_bound_response(context.key)
        if flight.result_is_dict and str(flight.result.get("status") or "").casefold() == "complete" and not flight.cacheable:
            with self._lock:
                self._metrics["singleflight_shared_failures"] += 1
                self._metrics["singleflight_unverified_waits"] += 1
            return self._unverified_response(context.key, flight.reason)
        if not flight.result_is_dict:
            with self._lock:
                self._metrics["singleflight_shared_failures"] += 1
                self._metrics["singleflight_unverified_waits"] += 1
            return self._unverified_response(context.key, "non_mapping_result")
        reused = flight.cacheable
        with self._lock:
            if reused:
                self._metrics["singleflight_shared_successes"] += 1
            else:
                self._metrics["singleflight_shared_failures"] += 1
        return self._decorate(
            flight.result,
            context.key,
            reused=reused,
            mode="singleflight",
            request_executed=False,
            cacheable=flight.cacheable,
            reason="singleflight_shared" if reused else flight.reason,
        )

    def _direct_response(self, result: Any, reason: str) -> Any:
        raw, is_dict = self._raw_result(result)
        if not is_dict:
            return raw
        with self._lock:
            self._metrics["cache_rejections"] += 1
        return self._decorate(
            raw,
            None,
            reused=False,
            mode="direct",
            request_executed=True,
            cacheable=False,
            reason=reason,
        )

    @staticmethod
    def _decorate(
        result: Any,
        key: DetailReuseKey | None,
        *,
        reused: bool,
        mode: str,
        request_executed: bool,
        cacheable: bool,
        reason: str,
    ) -> Any:
        if not isinstance(result, dict):
            return result
        response = deepcopy(result)
        response["_detail_reuse"] = {
            "reused": bool(reused),
            "request_made": bool(request_executed),
            "mode": mode,
            # Compatibility alias for adapters that used the initial draft name.
            "request_executed": bool(request_executed),
            "cacheable": bool(cacheable),
            "reason": reason,
            "key": key.token if key is not None else None,
        }
        return response

    @staticmethod
    def _timeout_response(key: DetailReuseKey) -> dict[str, Any]:
        result = {
            "status": "timeout",
            "detail": "",
            "source": "detail_reuse",
            "detail_url": key.detail_url,
            "error_type": "DetailReuseSingleflightTimeout",
        }
        return ReusingDetailHydrator._decorate(
            result,
            key,
            reused=False,
            mode="singleflight_timeout",
            request_executed=False,
            cacheable=False,
            reason="singleflight_wait_timeout",
        )

    @staticmethod
    def _company_bound_response(key: DetailReuseKey) -> dict[str, Any]:
        result = {
            "status": "identity_mismatch",
            "detail": "",
            "source": "detail_reuse",
            "detail_url": key.detail_url,
            "identity_status": "mismatch",
            "identity_evidence": ("reason:company_bound_response_not_shared",),
            "capture_evidence": {},
        }
        return ReusingDetailHydrator._decorate(
            result,
            key,
            reused=False,
            mode="singleflight_company_bound",
            request_executed=False,
            cacheable=False,
            reason="company_bound_response_not_shared",
        )

    @staticmethod
    def _unverified_response(key: DetailReuseKey, leader_reason: str) -> dict[str, Any]:
        result = {
            "status": "official_unverified",
            "detail": "",
            "source": "detail_reuse",
            "detail_url": key.detail_url,
            "identity_status": "unverified",
            "identity_evidence": (),
            "capture_evidence": {},
            "error_type": "DetailReuseUnverifiedLeader",
            "failure_reason": leader_reason,
        }
        return ReusingDetailHydrator._decorate(
            result,
            key,
            reused=False,
            mode="singleflight_unverified",
            request_executed=False,
            cacheable=False,
            reason="singleflight_unverified",
        )

    @staticmethod
    def _exception_response(key: DetailReuseKey, error: Exception) -> dict[str, Any]:
        result = {
            "status": "fetch_failed",
            "detail": "",
            "source": "detail_reuse",
            "detail_url": key.detail_url,
            "error_type": type(error).__name__,
            "attempts": ("detail_reuse:fetch_failed",),
        }
        return ReusingDetailHydrator._decorate(
            result,
            key,
            reused=False,
            mode="singleflight_exception",
            request_executed=False,
            cacheable=False,
            reason="singleflight_fetch_exception",
        )


def build_detail_reuse_key(job: Mapping[str, Any]) -> DetailReuseKey | None:
    """Return a controlled official detail key, or ``None`` when ineligible."""

    context, _reason = _build_key_context(job)
    return context.key if context is not None else None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DetailReuseKey",
    "ReusingDetailHydrator",
    "build_detail_reuse_key",
]
