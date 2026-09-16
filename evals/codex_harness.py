"""Offline golden-set comparison for Codex Harness runs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE_LABEL = re.compile(
    r"(?:bearer|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"cookie|session|password|secret|private[_-]?key|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)


class CodexHarnessResult(BaseModel):
    """Metrics from one golden-set case, with secret-bearing fields excluded."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: str = Field(min_length=1)
    tool_name: str | None = None
    success: bool = False
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error_code: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_common_result_shapes(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        _copy_alias(data, "case_id", "id", "case")
        _copy_alias(data, "tool_name", "selected_tool", "tool", "chosen_tool")
        _copy_alias(data, "latency_ms", "latency", "elapsed_ms", "duration_ms")
        usage = data.get("usage") or data.get("token_usage")
        if isinstance(usage, Mapping):
            _copy_alias_from(data, "input_tokens", usage, "input_tokens", "prompt_tokens")
            _copy_alias_from(data, "output_tokens", usage, "output_tokens", "completion_tokens")
            _copy_alias_from(data, "total_tokens", usage, "total_tokens")
        status = str(data.get("status") or data.get("outcome") or "").casefold()
        for alias in (
            "id",
            "case",
            "selected_tool",
            "tool",
            "chosen_tool",
            "latency",
            "elapsed_ms",
            "duration_ms",
            "usage",
            "token_usage",
            "status",
            "outcome",
        ):
            data.pop(alias, None)
        if "error_code" not in data and data.get("error"):
            data["error_code"] = "error"
        data.pop("error", None)
        if "success" not in data:
            data["success"] = status in {"success", "succeeded", "ok", "completed", "passed"}
        if status in {"error", "failed", "failure", "timeout"}:
            data["success"] = False
        return data

    @model_validator(mode="after")
    def normalize_metrics(self) -> "CodexHarnessResult":
        self.tool_name = _safe_label(self.tool_name)
        self.error_code = _safe_label(self.error_code)
        if self.error_code:
            self.success = False
        if (
            self.total_tokens is None
            and self.input_tokens is not None
            and self.output_tokens is not None
        ):
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class GoldenCaseComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    old_tool: str | None = None
    new_tool: str | None = None
    tool_changed: bool
    old_success: bool
    new_success: bool
    success_changed: bool
    old_latency_ms: float | None = None
    new_latency_ms: float | None = None
    latency_delta_ms: float | None = None
    old_tokens: int | None = None
    new_tokens: int | None = None
    token_delta: int | None = None

    @property
    def baseline_tool(self) -> str | None:
        return self.old_tool

    @property
    def candidate_tool(self) -> str | None:
        return self.new_tool

    @property
    def baseline_success(self) -> bool:
        return self.old_success

    @property
    def candidate_success(self) -> bool:
        return self.new_success


class GoldenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int = Field(ge=0)
    tool_selection_matches: int = Field(ge=0)
    tool_selection_match_rate: float = Field(ge=0, le=1)
    tool_changes: int = Field(ge=0)
    old_successes: int = Field(ge=0)
    new_successes: int = Field(ge=0)
    old_success_rate: float = Field(ge=0, le=1)
    new_success_rate: float = Field(ge=0, le=1)
    success_rate_delta: float
    old_mean_latency_ms: float = Field(ge=0)
    new_mean_latency_ms: float = Field(ge=0)
    latency_delta_ms: float
    old_total_tokens: int = Field(ge=0)
    new_total_tokens: int = Field(ge=0)
    token_delta: int
    old_token_observations: int = Field(ge=0)
    new_token_observations: int = Field(ge=0)

    @property
    def baseline_success_rate(self) -> float:
        return self.old_success_rate

    @property
    def candidate_success_rate(self) -> float:
        return self.new_success_rate

    @property
    def baseline_mean_latency_ms(self) -> float:
        return self.old_mean_latency_ms

    @property
    def candidate_mean_latency_ms(self) -> float:
        return self.new_mean_latency_ms

    @property
    def baseline_total_tokens(self) -> int:
        return self.old_total_tokens

    @property
    def candidate_total_tokens(self) -> int:
        return self.new_total_tokens


class GoldenEvalReport(BaseModel):
    """Structured, JSON-serializable comparison of two harness result sets."""

    model_config = ConfigDict(extra="forbid")

    evaluation: str = "codex_harness_golden"
    old_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    missing_from_old: list[str] = Field(default_factory=list)
    missing_from_new: list[str] = Field(default_factory=list)
    summary: GoldenSummary
    cases: list[GoldenCaseComparison] = Field(default_factory=list)

    def to_json(self, *, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    def write_json(self, path: Path | str, *, indent: int = 2) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")
        return target


def compare_golden_results(
    old_results: Iterable[CodexHarnessResult | Mapping[str, Any]],
    new_results: Iterable[CodexHarnessResult | Mapping[str, Any]],
) -> GoldenEvalReport:
    """Compare old and new results by case ID and aggregate safe metrics."""

    old = _result_map(old_results)
    new = _result_map(new_results)
    case_ids = sorted(set(old) | set(new))
    comparisons: list[GoldenCaseComparison] = []
    for case_id in case_ids:
        old_result = old.get(case_id) or CodexHarnessResult(case_id=case_id)
        new_result = new.get(case_id) or CodexHarnessResult(case_id=case_id)
        old_latency = old_result.latency_ms
        new_latency = new_result.latency_ms
        old_tokens = old_result.total_tokens
        new_tokens = new_result.total_tokens
        comparisons.append(
            GoldenCaseComparison(
                case_id=case_id,
                old_tool=old_result.tool_name,
                new_tool=new_result.tool_name,
                tool_changed=old_result.tool_name != new_result.tool_name,
                old_success=old_result.success,
                new_success=new_result.success,
                success_changed=old_result.success != new_result.success,
                old_latency_ms=old_latency,
                new_latency_ms=new_latency,
                latency_delta_ms=(new_latency - old_latency)
                if old_latency is not None and new_latency is not None
                else None,
                old_tokens=old_tokens,
                new_tokens=new_tokens,
                token_delta=(new_tokens - old_tokens)
                if old_tokens is not None and new_tokens is not None
                else None,
            )
        )
    summary = _summarize(comparisons)
    return GoldenEvalReport(
        old_count=len(old),
        new_count=len(new),
        missing_from_old=sorted(set(new) - set(old)),
        missing_from_new=sorted(set(old) - set(new)),
        summary=summary,
        cases=comparisons,
    )


def compare_results(
    old_results: Iterable[CodexHarnessResult | Mapping[str, Any]],
    new_results: Iterable[CodexHarnessResult | Mapping[str, Any]],
) -> GoldenEvalReport:
    return compare_golden_results(old_results, new_results)


def _result_map(
    results: Iterable[CodexHarnessResult | Mapping[str, Any]],
) -> dict[str, CodexHarnessResult]:
    mapped: dict[str, CodexHarnessResult] = {}
    for raw in results:
        result = (
            raw
            if isinstance(raw, CodexHarnessResult)
            else CodexHarnessResult.model_validate(raw)
        )
        if result.case_id in mapped:
            raise ValueError(f"duplicate Codex Harness case: {result.case_id}")
        mapped[result.case_id] = result
    return mapped


def _summarize(comparisons: list[GoldenCaseComparison]) -> GoldenSummary:
    cases = len(comparisons)
    old_latencies = [item.old_latency_ms for item in comparisons if item.old_latency_ms is not None]
    new_latencies = [item.new_latency_ms for item in comparisons if item.new_latency_ms is not None]
    old_tokens = [item.old_tokens for item in comparisons if item.old_tokens is not None]
    new_tokens = [item.new_tokens for item in comparisons if item.new_tokens is not None]
    old_successes = sum(item.old_success for item in comparisons)
    new_successes = sum(item.new_success for item in comparisons)
    old_success_rate = old_successes / cases if cases else 0.0
    new_success_rate = new_successes / cases if cases else 0.0
    old_mean_latency = mean(old_latencies) if old_latencies else 0.0
    new_mean_latency = mean(new_latencies) if new_latencies else 0.0
    old_total_tokens = sum(old_tokens)
    new_total_tokens = sum(new_tokens)
    return GoldenSummary(
        cases=cases,
        tool_selection_matches=sum(item.old_tool == item.new_tool for item in comparisons),
        tool_selection_match_rate=(
            sum(item.old_tool == item.new_tool for item in comparisons) / cases if cases else 0.0
        ),
        tool_changes=sum(item.tool_changed for item in comparisons),
        old_successes=old_successes,
        new_successes=new_successes,
        old_success_rate=old_success_rate,
        new_success_rate=new_success_rate,
        success_rate_delta=new_success_rate - old_success_rate,
        old_mean_latency_ms=old_mean_latency,
        new_mean_latency_ms=new_mean_latency,
        latency_delta_ms=new_mean_latency - old_mean_latency,
        old_total_tokens=old_total_tokens,
        new_total_tokens=new_total_tokens,
        token_delta=new_total_tokens - old_total_tokens,
        old_token_observations=len(old_tokens),
        new_token_observations=len(new_tokens),
    )


def _copy_alias(data: dict[str, Any], target: str, *aliases: str) -> None:
    if target in data:
        return
    for alias in aliases:
        if alias in data:
            data[target] = data[alias]
            return


def _copy_alias_from(
    data: dict[str, Any],
    target: str,
    source: Mapping[str, Any],
    *aliases: str,
) -> None:
    if target in data:
        return
    for alias in aliases:
        if alias in source:
            data[target] = source[alias]
            return


def _safe_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "[REDACTED]"
    candidate = value.strip()
    if not candidate:
        return None
    if _SENSITIVE_LABEL.search(candidate) or not _SAFE_LABEL.fullmatch(candidate):
        return "[REDACTED]"
    return candidate


HarnessResult = CodexHarnessResult
GoldenComparison = GoldenCaseComparison


__all__ = [
    "CodexHarnessResult",
    "GoldenCaseComparison",
    "GoldenComparison",
    "GoldenEvalReport",
    "GoldenSummary",
    "HarnessResult",
    "compare_golden_results",
    "compare_results",
]
