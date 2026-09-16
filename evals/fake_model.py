"""Deterministic, no-key model doubles used by the reliability evaluations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.matching.models import DeepSeekResponse


class FakeModelError(RuntimeError):
    """A fixture-controlled model failure with a reportable category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class FakeModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: Any
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    failure_category: str | None = Field(default=None, min_length=1)


class FakeModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    operation: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    system_chars: int = Field(ge=0)
    user_chars: int = Field(ge=0)
    failed: bool = False
    failure_category: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class FakeModel:
    """A keyed fake client that can be injected into model-backed services.

    The fake never reads an API key or environment variable.  A bound client
    exposes both ``complete`` (matching-service shape) and ``transport``
    (intent-router shape), so the same fixture can exercise both adapters.
    """

    def __init__(
        self,
        responses: Mapping[str, FakeModelSpec | Mapping[str, Any]],
        *,
        model: str = "fake-no-key",
    ) -> None:
        if not model.strip():
            raise ValueError("fake model name is required")
        if not responses:
            raise ValueError("at least one fake model response is required")
        self.model = model.strip()
        self.responses = {
            str(case_id): self._coerce_spec(spec)
            for case_id, spec in responses.items()
        }
        self.calls: list[FakeModelCall] = []

    @staticmethod
    def _coerce_spec(spec: FakeModelSpec | Mapping[str, Any]) -> FakeModelSpec:
        if isinstance(spec, FakeModelSpec):
            return spec
        raw = dict(spec)
        if "output" not in raw:
            controls = {key: raw.pop(key) for key in tuple(raw) if key in {
                "input_tokens", "output_tokens", "cost_usd", "failure_category"
            }}
            raw = {"output": raw, **controls}
        return FakeModelSpec.model_validate(raw)

    def bind(self, case_id: str, *, operation: str = "complete") -> "BoundFakeModel":
        if case_id not in self.responses:
            raise KeyError(f"unknown fake model case: {case_id}")
        return BoundFakeModel(self, case_id, operation=operation)

    def for_case(self, case_id: str, *, operation: str = "complete") -> "BoundFakeModel":
        return self.bind(case_id, operation=operation)

    def transport_for(self, case_id: str, *, operation: str = "intent"):
        return self.bind(case_id, operation=operation).transport

    def complete(
        self,
        *,
        case_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
        operation: str = "complete",
    ) -> DeepSeekResponse:
        del max_tokens
        return self._complete(
            case_id,
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def _spec(self, case_id: str) -> FakeModelSpec:
        try:
            return self.responses[case_id]
        except KeyError as exc:
            raise KeyError(f"unknown fake model case: {case_id}") from exc

    def _complete(
        self,
        case_id: str,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
    ) -> DeepSeekResponse:
        spec = self._spec(case_id)
        failed = bool(spec.failure_category)
        call = FakeModelCall(
            case_id=case_id,
            operation=operation,
            input_tokens=spec.input_tokens,
            output_tokens=spec.output_tokens,
            cost_usd=spec.cost_usd,
            system_chars=len(system_prompt),
            user_chars=len(user_prompt),
            failed=failed,
            failure_category=spec.failure_category,
        )
        self.calls.append(call)
        if spec.failure_category:
            raise FakeModelError(spec.failure_category)
        content = (
            spec.output
            if isinstance(spec.output, str)
            else json.dumps(spec.output, ensure_ascii=False, sort_keys=True)
        )
        return DeepSeekResponse(
            content=content,
            model=self.model,
            input_tokens=spec.input_tokens,
            output_tokens=spec.output_tokens,
        )

    def last_call(self, case_id: str) -> FakeModelCall | None:
        for call in reversed(self.calls):
            if call.case_id == case_id:
                return call
        return None


class BoundFakeModel:
    """A case-scoped adapter suitable for dependency injection."""

    def __init__(self, parent: FakeModel, case_id: str, *, operation: str):
        self._parent = parent
        self.case_id = case_id
        self.operation = operation
        self.model = parent.model

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
    ) -> DeepSeekResponse:
        del max_tokens
        return self._parent._complete(
            self.case_id,
            operation=self.operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def transport(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        del endpoint, headers, timeout
        messages = payload.get("messages") or []
        user_prompt = str(messages[-1].get("content") or "") if messages else ""
        response = self._parent._complete(
            self.case_id,
            operation=self.operation,
            system_prompt=str(payload.get("system") or ""),
            user_prompt=user_prompt,
        )
        return {
            "model": response.model,
            "content": [{"type": "text", "text": response.content}],
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
        }

    def classify_mail(self, *, subject: str, body: str) -> dict[str, Any]:
        response = self.complete(
            system_prompt="Classify the synthetic recruitment email as JSON.",
            user_prompt=json.dumps(
                {"subject": subject, "body": body},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        value = json.loads(response.content)
        if not isinstance(value, dict):
            raise ValueError("fake mail classification is not an object")
        return value


__all__ = [
    "BoundFakeModel",
    "FakeModel",
    "FakeModelCall",
    "FakeModelError",
    "FakeModelSpec",
]
