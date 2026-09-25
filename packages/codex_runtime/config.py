from __future__ import annotations

from enum import StrEnum
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from packages.model_policy import official_base, official_model


class RestartPolicy(StrEnum):
    """When an unexpected server exit is eligible for a later restart."""

    NEVER = "never"
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


RestartStrategy = RestartPolicy


class CodexRuntimeConfig(BaseModel):
    """Configuration needed to launch one local Codex app-server process."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: tuple[str, ...] = Field(min_length=1)
    working_dir: Path | None = None
    skill_roots: tuple[Path, ...] = ()
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    restart_policy: RestartPolicy = RestartPolicy.ON_FAILURE
    max_restarts: int = Field(default=3, ge=0)
    restart_backoff_seconds: float = Field(default=0.0, ge=0)
    provider: str = Field(default="default", min_length=1)
    model: str = Field(default="default", min_length=1)
    client_name: str = Field(default="recruitops_agent", min_length=1)
    client_title: str = Field(default="RecruitOps Agent", min_length=1)
    client_version: str = Field(default="0.1.0", min_length=1)
    experimental_api: bool = True
    diagnostics_method: str = Field(default="server/diagnostics", min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        command = tuple(item.strip() for item in value)
        if any(not item for item in command):
            raise ValueError("command entries must not be blank")
        return command


RuntimeConfig = CodexRuntimeConfig
CodexConfig = CodexRuntimeConfig


class CodexHomeConfig(BaseModel):
    """Secret-free project-owned Codex provider and MCP configuration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    provider_name: str = Field(default="DeepSeek", min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    reasoning_effort: str = Field(default="high", pattern=r"^(low|medium|high|xhigh|max)$")
    model_context_window: int = Field(default=1_000_000, ge=16_384)
    model_auto_compact_token_limit: int = Field(default=96_000, ge=8_192)
    request_max_retries: int = Field(default=2, ge=0, le=10)
    stream_max_retries: int = Field(default=3, ge=0, le=10)
    stream_idle_timeout_ms: int = Field(default=120_000, ge=1_000, le=900_000)
    mcp_command: str = Field(min_length=1)
    mcp_args: tuple[str, ...] = ()
    mcp_env_vars: tuple[str, ...] = ()
    mcp_default_tools_approval_mode: str = Field(
        default="approve",
        pattern=r"^(auto|prompt|writes|approve)$",
    )

    @field_validator("mcp_env_vars")
    @classmethod
    def validate_mcp_env_vars(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(name.strip() for name in value)
        if any(not name or not name.replace("_", "").isalnum() for name in names):
            raise ValueError("MCP environment variable names must be non-blank identifiers")
        if len(names) != len(set(names)):
            raise ValueError("MCP environment variable names must be unique")
        return names

    def render_toml(self) -> str:
        official_base(self.base_url)
        official_model(self.model)
        if self.model_auto_compact_token_limit >= self.model_context_window:
            raise ValueError("auto-compaction limit must be smaller than the model context window")
        quote = lambda value: json.dumps(value, ensure_ascii=False)
        args = ", ".join(quote(value) for value in self.mcp_args)
        env_vars = ", ".join(quote(value) for value in self.mcp_env_vars)
        return "\n".join(
            (
                f"model = {quote(self.model)}",
                f"model_provider = {quote(self.provider_id)}",
                f"model_reasoning_effort = {quote({'medium': 'high', 'xhigh': 'max'}.get(self.reasoning_effort, self.reasoning_effort))}",
                f"model_context_window = {self.model_context_window}",
                f"model_auto_compact_token_limit = {self.model_auto_compact_token_limit}",
                'approval_policy = "on-request"',
                'sandbox_mode = "read-only"',
                "",
                f"[model_providers.{self.provider_id}]",
                f"name = {quote(self.provider_name)}",
                f"base_url = {quote(self.base_url.rstrip('/'))}",
                f"env_key = {quote(self.api_key_env)}",
                'wire_api = "responses"',
                "requires_openai_auth = false",
                f"request_max_retries = {self.request_max_retries}",
                f"stream_max_retries = {self.stream_max_retries}",
                f"stream_idle_timeout_ms = {self.stream_idle_timeout_ms}",
                "",
                "[mcp_servers.recruitops]",
                f"command = {quote(self.mcp_command)}",
                f"args = [{args}]",
                f"env_vars = [{env_vars}]",
                (
                    "default_tools_approval_mode = "
                    f"{quote(self.mcp_default_tools_approval_mode)}"
                ),
                "required = true",
                "startup_timeout_sec = 20",
                "tool_timeout_sec = 120",
                "",
            )
        )

    def write(self, codex_home: Path) -> Path:
        target = codex_home.expanduser().resolve(strict=False) / "config.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_toml(), encoding="utf-8")
        return target


__all__ = [
    "CodexConfig",
    "CodexRuntimeConfig",
    "CodexHomeConfig",
    "RestartPolicy",
    "RestartStrategy",
    "RuntimeConfig",
]
