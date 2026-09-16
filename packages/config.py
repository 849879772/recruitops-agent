from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UNIFIED_MODEL = "deepseek-flash"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="RECRUITOPS_",
        extra="ignore",
    )

    env: str = "development"
    agent_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[1]
    )
    source_root: Path = Field(default=Path("D:/秋招系统"))
    source_python_executable: str = "python"
    database_url: str = "postgresql+psycopg://recruitops:recruitops@localhost:5433/recruitops"
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    api_token: str = ""
    write_enabled: bool = False
    backup_root: Path = Path(".data/backups")
    checkpoint_mode: str = "memory"
    trace_path: Path = Path(".data/traces.jsonl")
    codex_trace_path: Path = Path(".data/codex-traces.jsonl")
    codex_runtime_enabled: bool = False
    codex_cli_version: str = "0.149.0"
    codex_command: tuple[str, ...] = ("codex", "app-server")
    codex_home: Path = Path(".data/codex-home")
    codex_startup_timeout_seconds: float = 15.0
    codex_model_provider_id: str = "deepseek"
    model_api_base_url: str | None = None
    codex_model_base_url: str = "https://api.deepseek.com"
    codex_model_api_key_env: str = "RECRUITOPS_LLM_API_KEY"
    codex_model: str = "deepseek-flash"
    codex_reasoning_effort: str = "high"
    codex_model_context_window: int = 1_000_000
    codex_model_auto_compact_token_limit: int = 96_000
    automation_enabled: bool = True
    automation_poll_seconds: float = 10.0
    automation_run_timeout_seconds: float = 600.0
    llm_enabled: bool = False
    job_analysis_enabled: bool = True
    llm_endpoint: str = "https://api.deepseek.com/anthropic/v1/messages"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 45.0
    llm_max_tokens: int = 400
    llm_tool_calling_enabled: bool = True
    llm_agent_max_tokens: int = 1_200
    llm_matching_max_tokens: int = 2000
    llm_matching_thinking_enabled: bool = False
    llm_matching_reasoning_effort: str = "high"
    vision_enabled: bool = True
    vision_model: str = "deepseek-flash"
    vision_endpoint: str = "https://api.deepseek.com/chat/completions"
    vision_max_image_bytes: int = 6 * 1024 * 1024
    vision_timeout_seconds: float = 45.0
    crawl_max_concurrency: int = 4
    crawl_company_timeout_seconds: float = 300.0
    match_max_concurrency: int = 4
    match_checkpoint_batch_size: int = 25
    discovery_enabled: bool = True
    offerbiu_max_pages: int = 150
    offerbiu_page_size: int = 50
    offerbiu_delay_seconds: float = 0.05
    offline_reconciliation_enabled: bool = True
    offline_grace_runs: int = 2
    offline_grace_days: float = 3.0
    embedding_endpoint: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = 1024
    knowledge_embedding_endpoint: str = ""
    knowledge_embedding_api_key: str = ""
    knowledge_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    knowledge_embedding_dimension: int = 1024
    mail_enabled: bool = False
    mail_imap_host: str = "imap.163.com"
    mail_imap_port: int = 993
    mail_imap_username: str = ""
    mail_imap_password: str = ""
    mail_imap_mailbox: str = "INBOX"
    mail_sync_on_startup: bool = True
    mail_sync_ttl_seconds: int = 300

    @model_validator(mode="after")
    def unified_flash_model(self):
        from urllib.parse import urlsplit
        base = (self.model_api_base_url or self.codex_model_base_url).strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Model API base must be an HTTP/HTTPS URL without credentials, query or fragment")
        self.model_api_base_url = base
        self.codex_model_base_url = base
        self.llm_endpoint = base + "/anthropic/v1/messages"
        self.vision_endpoint = base + "/chat/completions"
        self.llm_model = self.codex_model = self.vision_model = UNIFIED_MODEL
        return self

    @property
    def companies_config(self) -> Path:
        return self.agent_root / "config" / "companies.yaml"

    @property
    def candidate_profile_config(self) -> Path:
        local = self.agent_root / ".data" / "settings" / "candidate_profile.yaml"
        if local.is_file():
            return local
        return self.agent_root / "config" / "candidate_profile.yaml"

    def source_database(self) -> Path:
        return self.source_root / "data" / "jobs.db"

    @property
    def source_applications(self) -> Path:
        return self.source_root / "data" / "applications.json"

    @property
    def source_config(self) -> Path:
        return self.source_root / "config.yaml"


@lru_cache
def get_settings() -> Settings:
    import json
    settings = Settings()
    local = settings.agent_root / ".data" / "settings" / "preferences.json"
    if local.is_file():
        from packages.user_settings import CONFIG_FIELDS
        overrides = json.loads(local.read_text(encoding="utf-8"))
        values = settings.model_dump()
        values.update({key: value for key, value in overrides.items() if key in CONFIG_FIELDS})
        if "model_api_base_url" not in overrides and overrides.get("codex_model_base_url"):
            values["model_api_base_url"] = overrides["codex_model_base_url"]
        settings = Settings.model_validate(values)
    return settings
