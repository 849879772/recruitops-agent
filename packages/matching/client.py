from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import DeepSeekResponse


DEFAULT_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class DeepSeekClientError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


Transport = Callable[[str, dict[str, str], dict[str, Any], float], Mapping[str, Any]]


class DeepSeekClientProtocol(Protocol):
    model: str

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> DeepSeekResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _validated_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if parsed.scheme != "https" and not local_http:
        raise ValueError("DeepSeek endpoint must use HTTPS or local HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("DeepSeek endpoint cannot contain credentials, query, or fragment")
    return endpoint


def _default_transport(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise DeepSeekClientError(f"http_{exc.code}") from exc
    except (URLError, TimeoutError, OSError):
        raise DeepSeekClientError("transport_failed") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeepSeekClientError("response_invalid") from None
    if not isinstance(value, Mapping):
        raise DeepSeekClientError("response_invalid")
    return value


def _text_content(data: Mapping[str, Any]) -> str:
    if data.get("type") == "error":
        raise DeepSeekClientError("provider_error")
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        raise DeepSeekClientError("response_invalid")
    text = "".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
    ).strip()
    if not text:
        if data.get("stop_reason") == "max_tokens":
            raise DeepSeekClientError("response_truncated")
        raise DeepSeekClientError("response_empty")
    return text


def _usage(data: Mapping[str, Any], key: str) -> int | None:
    usage = data.get("usage")
    value = usage.get(key) if isinstance(usage, Mapping) else None
    return value if isinstance(value, int) and value >= 0 else None


class DeepSeekClient:
    """Small injectable client; matching tests can provide a transport fake."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 45.0,
        max_tokens: int = 1000,
        thinking_enabled: bool = False,
        reasoning_effort: str = "high",
        transport: Transport | None = None,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.5,
        api_style: str = "anthropic",
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if not model.strip():
            raise ValueError("DeepSeek model is required")
        self.api_key = api_key.strip()
        self.model = model.strip()
        if api_style not in {"anthropic", "openai"}:
            raise ValueError("api_style must be anthropic or openai")
        self.api_style = api_style
        self.endpoint = _validated_endpoint(endpoint)
        self.timeout = max(1.0, min(float(timeout), 180.0))
        self.max_tokens = max(128, min(int(max_tokens), 8_000))
        if reasoning_effort not in {"low", "medium", "high", "max"}:
            raise ValueError("reasoning_effort must be low, medium, high, or max")
        self.thinking_enabled = bool(thinking_enabled)
        self.reasoning_effort = reasoning_effort
        self.transport = transport or _default_transport
        self.max_attempts = max(1, min(int(max_attempts), 4))
        self.retry_backoff_seconds = max(0.0, min(float(retry_backoff_seconds), 5.0))

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> DeepSeekResponse:
        return self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            thinking_enabled=self.thinking_enabled,
        )

    def complete_with_thinking(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> DeepSeekResponse:
        """Retry a structurally invalid result with explicit reasoning enabled."""

        return self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            thinking_enabled=True,
        )

    def complete_structured(self, *, system_prompt: str, user_prompt: str,
                            schema: dict[str, Any], max_tokens: int | None = None) -> DeepSeekResponse:
        return self._complete(system_prompt=system_prompt, user_prompt=user_prompt,
                              max_tokens=max_tokens, thinking_enabled=False, output_schema=schema)

    def _complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None,
        thinking_enabled: bool,
        output_schema: dict[str, Any] | None = None,
    ) -> DeepSeekResponse:
        payload = {
            "model": self.model,
            "max_tokens": max(128, min(int(max_tokens or self.max_tokens), self.max_tokens)),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "reasoning": {
                "effort": self.reasoning_effort if thinking_enabled else "none"
            },
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "stream": False,
        }
        if thinking_enabled:
            payload["output_config"] = {"effort": self.reasoning_effort}
        if output_schema is not None:
            schema = dict(output_schema)
            definitions = schema.pop("$defs", {})
            payload["tools"] = [{"name": "submit_mail_analysis", "description": "Return structured mail evidence only; no external action.",
                "input_schema": {"type": "object", "properties": {"result": schema},
                                 "required": ["result"], "additionalProperties": False, "$defs": definitions}}]
            payload["tool_choice"] = {"type": "tool", "name": "submit_mail_analysis"}
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "x-api-key": self.api_key,
        }
        if self.api_style == "openai":
            structured_prompt = system_prompt
            if output_schema is not None:
                structured_prompt += (
                    "\nReturn one JSON object matching this complete schema. "
                    "Include every required field and every value/evidence pair; use null or empty arrays "
                    "only where the schema permits. Do not omit fields to save tokens. Schema: "
                    + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
                )
            payload = {
                "model": self.model,
                "max_tokens": payload["max_tokens"],
                "messages": [
                    {"role": "system", "content": structured_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            # Chat Completions uses DeepSeek's thinking toggle, not the
            # Responses/Anthropic reasoning object. Also support named
            # DeepSeek models behind compatible gateways without adding
            # provider-specific fields to unrelated OpenAI-compatible models.
            if (urlparse(self.endpoint).hostname == "api.deepseek.com"
                    or self.model.casefold().split("/")[-1].startswith("deepseek-")):
                payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
                if thinking_enabled:
                    payload["reasoning_effort"] = "max" if self.reasoning_effort == "max" else "high"
            headers = {"Authorization": f"Bearer {self.api_key}",
                       "content-type": "application/json"}
        for attempt in range(1, self.max_attempts + 1):
            try:
                data = self.transport(self.endpoint, headers, payload, self.timeout)
                if self.api_style == "openai":
                    choices = data.get("choices")
                    choice = choices[0] if isinstance(choices, list) and choices else None
                    if not isinstance(choice, Mapping):
                        raise DeepSeekClientError("response_invalid")
                    if choice.get("finish_reason") == "length":
                        raise DeepSeekClientError("response_truncated")
                    message = choice.get("message")
                    content = message.get("content") if isinstance(message, Mapping) else None
                    if not isinstance(content, str) or not content.strip():
                        raise DeepSeekClientError("response_empty")
                    return DeepSeekResponse(
                        content=content.strip(), model=str(data.get("model") or self.model),
                        input_tokens=_usage(data, "prompt_tokens"),
                        output_tokens=_usage(data, "completion_tokens"),
                    )
                elif output_schema is not None:
                    blocks = data.get("content")
                    calls = [b for b in blocks if isinstance(b, Mapping) and b.get("type") == "tool_use"] if isinstance(blocks, list) else []
                    if (data.get("stop_reason") == "max_tokens" or len(calls) != 1
                            or calls[0].get("name") != "submit_mail_analysis"
                            or not isinstance(calls[0].get("input"), Mapping)
                            or set(calls[0]["input"]) != {"result"}):
                        raise DeepSeekClientError("structured_response_invalid")
                    content = json.dumps(calls[0]["input"]["result"], ensure_ascii=False)
                else:
                    content = _text_content(data)
                return DeepSeekResponse(
                    content=content,
                    model=str(data.get("model") or self.model),
                    input_tokens=_usage(data, "input_tokens"),
                    cache_creation_input_tokens=_usage(
                        data, "cache_creation_input_tokens"
                    ),
                    cache_read_input_tokens=_usage(data, "cache_read_input_tokens"),
                    output_tokens=_usage(data, "output_tokens"),
                )
            except DeepSeekClientError as exc:
                transient = (
                    exc.code in {"response_empty", "transport_failed", "http_429"}
                    or exc.code.startswith("http_5")
                )
                if not transient or attempt >= self.max_attempts:
                    raise
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * attempt)
        raise DeepSeekClientError("transport_failed")


__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_ENDPOINT",
    "DeepSeekClient",
    "DeepSeekClientError",
    "DeepSeekClientProtocol",
    "Transport",
]
