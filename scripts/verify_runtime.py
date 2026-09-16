"""Verify the local RecruitOps API runtime boundary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_API_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_TIMEOUT_SECONDS = 5.0
MISSING_APPROVAL_ID = "__recruitops_runtime_verification_missing__"
READY_PATH = "/ready"
APPROVAL_PATH = f"/api/approvals/{MISSING_APPROVAL_ID}/approve"


class LocalApiUrlError(ValueError):
    """The configured API URL is outside the local HTTP boundary."""


class ApiTransportError(RuntimeError):
    """The local API could not be reached."""


@dataclass(frozen=True)
class HttpResponse:
    """The response data needed by the verifier, without retaining raw text."""

    status_code: int
    json_body: object | None = None


HttpCall = Callable[..., HttpResponse]


def _local_api_base_url(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise LocalApiUrlError from exc

    if (
        parsed.scheme.casefold() != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise LocalApiUrlError
    return candidate.rstrip("/")


def _decode_json(raw: bytes) -> object | None:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _urllib_http_call(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
) -> HttpResponse:
    """Call only the already-validated local URL, without using proxy settings."""

    request = Request(url, method=method, headers=dict(headers))
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status_code=int(response.getcode()),
                json_body=_decode_json(response.read()),
            )
    except HTTPError as exc:
        return HttpResponse(status_code=int(exc.code), json_body=_decode_json(exc.read()))
    except (OSError, TimeoutError, URLError, ValueError) as exc:
        raise ApiTransportError from exc


def _request(
    http_call: HttpCall,
    *,
    base_url: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[HttpResponse | None, str | None]:
    try:
        response = http_call(
            method,
            f"{base_url}{path}",
            headers=headers,
            timeout=timeout,
        )
    except ApiTransportError:
        return None, "api_unavailable"
    except Exception:
        return None, "http_call_failed"
    if not isinstance(response, HttpResponse):
        return None, "invalid_http_response"
    return response, None


def _status_check(
    response: HttpResponse | None,
    error: str | None,
    *,
    expected_status: int,
) -> dict[str, Any]:
    observed_status = response.status_code if response is not None else None
    result: dict[str, Any] = {
        "ok": observed_status == expected_status,
        "expected_status": expected_status,
        "observed_status": observed_status,
    }
    if error:
        result["error"] = error
    return result


def _ready_check(
    response: HttpResponse | None,
    error: str | None,
) -> dict[str, Any]:
    result = _status_check(response, error, expected_status=200)
    if not result["ok"]:
        return result
    if not isinstance(response.json_body, dict) or response.json_body.get("status") != "ready":
        result["ok"] = False
        result["error"] = "invalid_readiness_payload"
    return result


def _public_write_enabled(body: object | None) -> bool | None:
    """Read write state only from fields intentionally returned by a public API."""

    if not isinstance(body, dict):
        return None
    value = body.get("write_enabled")
    if isinstance(value, bool):
        return value
    mode = body.get("mode")
    if mode == "read_only":
        return False
    if mode == "approval_gated":
        return True
    return None


def _write_enabled_result(
    response: HttpResponse | None,
    error: str | None,
) -> dict[str, Any]:
    if response is None:
        return {"status": "error", "error": error or "api_unavailable"}
    value = _public_write_enabled(response.json_body)
    if value is None:
        return {"status": "unsupported"}
    return {"status": "supported", "value": value, "source": "ready"}


def verify_runtime(
    *,
    api_base_url: str = DEFAULT_API_BASE_URL,
    api_token: str = "",
    http_call: HttpCall | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run non-mutating checks against the local RecruitOps HTTP API.

    ``http_call`` receives ``(method, url, headers=..., timeout=...)`` and must
    return :class:`HttpResponse`; this keeps tests independent of a live API.
    """

    base_url = _local_api_base_url(api_base_url)
    if timeout <= 0:
        raise ValueError
    call = http_call or _urllib_http_call

    ready_response, ready_error = _request(
        call,
        base_url=base_url,
        method="GET",
        path=READY_PATH,
        headers={},
        timeout=timeout,
    )
    unauthenticated_response, unauthenticated_error = _request(
        call,
        base_url=base_url,
        method="POST",
        path=APPROVAL_PATH,
        headers={},
        timeout=timeout,
    )
    token = api_token.strip()
    if token:
        authenticated_response, authenticated_error = _request(
            call,
            base_url=base_url,
            method="POST",
            path=APPROVAL_PATH,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        authenticated_check = _status_check(
            authenticated_response,
            authenticated_error,
            expected_status=404,
        )
    else:
        authenticated_check = {
            "ok": False,
            "expected_status": 404,
            "observed_status": None,
            "error": "missing_local_token",
        }

    checks = {
        "ready": _ready_check(ready_response, ready_error),
        "unauthenticated_approval": _status_check(
            unauthenticated_response,
            unauthenticated_error,
            expected_status=401,
        ),
        "authenticated_missing_approval": authenticated_check,
    }
    return {
        "ok": all(check["ok"] for check in checks.values()),
        "checks": checks,
        "write_enabled": _write_enabled_result(ready_response, ready_error),
    }


def _configured_api_token() -> str:
    token = os.environ.get("RECRUITOPS_API_TOKEN", "")
    if token:
        return token.strip()

    dotenv_paths = (Path.cwd() / ".env", ROOT / ".env")
    for path in dict.fromkeys(dotenv_paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            key, separator, value = stripped.partition("=")
            if separator and key.strip() == "RECRUITOPS_API_TOKEN":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value.strip()
    return ""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("RECRUITOPS_API_BASE_URL", DEFAULT_API_BASE_URL),
        help="Local HTTP RecruitOps API base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-request timeout in seconds (default: %(default)s).",
    )
    return parser.parse_args(argv)


def _error_result(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": code,
        "checks": {},
        "write_enabled": {"status": "unsupported"},
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    http_call: HttpCall | None = None,
) -> int:
    try:
        args = parse_args(argv)
        result = verify_runtime(
            api_base_url=args.api_base_url,
            api_token=_configured_api_token(),
            http_call=http_call,
            timeout=args.timeout,
        )
    except LocalApiUrlError:
        result = _error_result("invalid_local_api_url")
    except ValueError:
        result = _error_result("invalid_timeout")
    except RuntimeError:
        result = _error_result("local_configuration_unavailable")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
