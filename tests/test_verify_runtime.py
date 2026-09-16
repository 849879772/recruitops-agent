from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import verify_runtime as verifier


def _http_call_factory(*, ready_body: object | None = None):
    calls: list[dict[str, Any]] = []

    def http_call(method: str, url: str, *, headers, timeout: float):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if method == "GET" and url.endswith("/ready"):
            return verifier.HttpResponse(200, ready_body or {"status": "ready", "mode": "read_only"})
        if not headers:
            return verifier.HttpResponse(401, {"detail": "unauthenticated"})
        return verifier.HttpResponse(404, {"detail": "approval not found"})

    return http_call, calls


def test_verify_runtime_checks_local_api_contract_without_leaking_token() -> None:
    http_call, calls = _http_call_factory()

    result = verifier.verify_runtime(
        api_base_url="http://127.0.0.1:8010/",
        api_token="local-secret",
        http_call=http_call,
    )

    assert result["ok"] is True
    assert result["checks"]["ready"] == {
        "ok": True,
        "expected_status": 200,
        "observed_status": 200,
    }
    assert result["checks"]["unauthenticated_approval"]["observed_status"] == 401
    assert result["checks"]["authenticated_missing_approval"]["observed_status"] == 404
    assert result["write_enabled"] == {
        "status": "supported",
        "value": False,
        "source": "ready",
    }
    assert calls == [
        {
            "method": "GET",
            "url": "http://127.0.0.1:8010/ready",
            "headers": {},
            "timeout": 5.0,
        },
        {
            "method": "POST",
            "url": "http://127.0.0.1:8010/api/approvals/__recruitops_runtime_verification_missing__/approve",
            "headers": {},
            "timeout": 5.0,
        },
        {
            "method": "POST",
            "url": "http://127.0.0.1:8010/api/approvals/__recruitops_runtime_verification_missing__/approve",
            "headers": {"Authorization": "Bearer local-secret"},
            "timeout": 5.0,
        },
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "local-secret" not in serialized
    assert "postgres" not in serialized
    assert "?" not in serialized


def test_write_enabled_is_unsupported_when_public_api_does_not_expose_it() -> None:
    http_call, _calls = _http_call_factory(ready_body={"status": "ready"})

    result = verifier.verify_runtime(api_token="local-secret", http_call=http_call)

    assert result["ok"] is True
    assert result["write_enabled"] == {"status": "unsupported"}


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8010",
        "http://example.com:8010",
        "http://user:pass@127.0.0.1:8010",
        "http://127.0.0.1:8010?token=secret",
        "http://127.0.0.1:8010/api",
    ],
)
def test_verify_runtime_rejects_non_local_or_credentialed_urls(url: str) -> None:
    with pytest.raises(verifier.LocalApiUrlError):
        verifier.verify_runtime(
            api_base_url=url,
            api_token="local-secret",
            http_call=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
        )


def test_missing_local_token_is_reported_without_authenticated_request() -> None:
    http_call, calls = _http_call_factory()

    result = verifier.verify_runtime(http_call=http_call)

    assert result["ok"] is False
    assert result["checks"]["authenticated_missing_approval"] == {
        "ok": False,
        "expected_status": 404,
        "observed_status": None,
        "error": "missing_local_token",
    }
    assert len(calls) == 2


def test_configured_token_can_be_read_from_local_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("RECRUITOPS_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        'RECRUITOPS_API_TOKEN="dotenv-secret"\n',
        encoding="utf-8",
    )

    assert verifier._configured_api_token() == "dotenv-secret"


def test_main_emits_json_only_and_uses_injected_http(monkeypatch, capsys) -> None:
    http_call, _calls = _http_call_factory()
    monkeypatch.setenv("RECRUITOPS_API_TOKEN", "local-secret")

    assert verifier.main([], http_call=http_call) == 0

    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["ok"] is True
    assert "local-secret" not in output.out
    assert "RECRUITOPS_API_TOKEN" not in output.out
