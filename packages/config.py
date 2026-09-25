from functools import lru_cache
import json
import os
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.user_settings import DEFAULT_ON_CAPABILITY_FIELDS
from packages.model_policy import DEEPSEEK_BASE, official_base, official_model

UNIFIED_MODEL = "deepseek-flash"
DESKTOP_CAPABILITY_FIELDS = (
    "llm_enabled", "job_analysis_enabled", "codex_runtime_enabled", "mail_enabled",
    "mail_sync_on_startup", "automation_enabled", "vision_enabled",
)
DEFAULT_OFFERBIU_INDUSTRY_GROUPS = (
    "internet-tech",
    "manufacturing-equipment",
    "auto-transport-equipment",
)
OFFERBIU_INDUSTRY_GROUP_OPTIONS = (
    ("internet-tech", "互联网/软件/AI"),
    ("semiconductor-hardware", "半导体/电子硬件"),
    ("finance", "金融/银行/证券/保险"),
    ("professional-services", "咨询/法律/专业服务"),
    ("manufacturing-equipment", "制造/装备/工业自动化"),
    ("auto-transport-equipment", "汽车/新能源车/交通设备"),
    ("energy-chemical-environment", "能源/电力/化工/环保"),
    ("biotech-healthcare", "生物医药/医疗健康"),
    ("consumer-retail", "消费/零售/电商/快消"),
    ("construction-real-estate", "建筑地产/市政工程"),
    ("media-education-culture", "传媒/文娱/教育"),
    ("public-research-nonprofit", "政府/事业单位/科研/公益"),
    ("logistics-supply-chain", "物流/供应链/交通运输"),
    ("other", "其他/待归类"),
)
_OFFERBIU_INDUSTRY_GROUP_CODES = frozenset(
    code for code, _label in OFFERBIU_INDUSTRY_GROUP_OPTIONS
)


def anonymous_profile_payload() -> dict[str, object]:
    """Return a fresh, developer-neutral profile for first-run bootstrap."""

    return {
        "profile": {
            "degree": None,
            "job_type": None,
            "direction": None,
            "skills": [],
            "matching": {
                "title_keywords": [],
                "excluded_title_keywords": [],
                "direction_policy": "parallel",
                "primary_directions": [],
                "secondary_directions": [],
                "project_evidence": [],
                "supporting_skills": [],
                "learning_targets": [],
                "unverified_skills": [],
            },
        }
    }


def _write_yaml_if_missing(path: Path, payload: dict[str, object]) -> bool:
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def ensure_anonymous_configuration(settings: "Settings") -> dict[str, bool]:
    """Create only missing, anonymous config files; never copy example data."""

    companies_created = _write_yaml_if_missing(
        settings.companies_config,
        {"companies": []},
    )
    profile_path = Path(settings.agent_root) / "config" / "candidate_profile.yaml"
    local_profile = Path(settings.agent_root) / ".data" / "settings" / "candidate_profile.yaml"
    profile_created = False
    if not local_profile.is_file():
        profile_created = _write_yaml_if_missing(profile_path, anonymous_profile_payload())
    return {
        "companies_created": companies_created,
        "profile_created": profile_created,
    }


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
    source_root: Path = Field(default=Path(".data/legacy-source"))
    source_python_executable: str = "python"
    # A fresh checkout must not probe a developer's local PostgreSQL instance.
    # Desktop/runtime launchers may supply an isolated database URL explicitly.
    database_url: str = "sqlite+pysqlite:///./.data/recruitops.sqlite"
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
    model_provider_name: str = "DeepSeek"
    model_api_style: Literal["anthropic"] = "anthropic"
    model_connection_migration_required: bool = False
    model_name: str = UNIFIED_MODEL
    model_api_base_url: str | None = None
    codex_model_base_url: str = "https://api.deepseek.com"
    codex_model_api_key_env: str = "RECRUITOPS_LLM_API_KEY"
    codex_model: str = "deepseek-flash"
    codex_reasoning_effort: str = "high"
    codex_model_context_window: int = 1_000_000
    codex_model_auto_compact_token_limit: int = 96_000
    automation_enabled: bool = "automation_enabled" in DEFAULT_ON_CAPABILITY_FIELDS
    automation_poll_seconds: float = 10.0
    automation_run_timeout_seconds: float = 600.0
    llm_enabled: bool = False
    job_analysis_enabled: bool = "job_analysis_enabled" in DEFAULT_ON_CAPABILITY_FIELDS
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
    crawl_max_concurrency: int = Field(default=10, ge=1)
    detail_max_concurrency: int = Field(default=10, ge=1)
    browser_max_concurrency: int = Field(default=6, ge=1, le=6)
    crawl_company_timeout_seconds: float = 300.0
    match_max_concurrency: int = Field(default=6, ge=1)
    match_checkpoint_batch_size: int = 25
    discovery_enabled: bool = True
    offerbiu_max_pages: int = 150
    offerbiu_page_size: int = 50
    offerbiu_delay_seconds: float = 0.05
    offerbiu_industry_groups: list[str] = Field(
        default_factory=lambda: list(DEFAULT_OFFERBIU_INDUSTRY_GROUPS)
    )
    offline_reconciliation_enabled: bool = True
    offline_grace_runs: int = 2
    offline_grace_days: float = 3.0
    embedding_endpoint: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = 1024
    mail_enabled: bool = "mail_enabled" in DEFAULT_ON_CAPABILITY_FIELDS
    mail_imap_host: str = "imap.163.com"
    mail_imap_port: int = 993
    mail_imap_username: str = ""
    mail_imap_password: str = ""
    mail_imap_mailbox: str = "INBOX"
    mail_sync_on_startup: bool = True
    mail_sync_ttl_seconds: int = 300

    @field_validator("offerbiu_industry_groups", mode="before")
    @classmethod
    def normalize_offerbiu_industry_groups(cls, value: object) -> list[str]:
        if value is None:
            values = list(DEFAULT_OFFERBIU_INDUSTRY_GROUPS)
        elif isinstance(value, str):
            values = [item.strip() for item in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            values = [str(item).strip() for item in value]
        else:
            raise ValueError("offerbiu_industry_groups must be a list of codes")
        values = list(dict.fromkeys(item for item in values if item))
        if not values:
            raise ValueError("at least one OfferBiu industry group is required")
        unknown = sorted(set(values) - _OFFERBIU_INDUSTRY_GROUP_CODES)
        if unknown:
            raise ValueError("unsupported OfferBiu industry group: " + ", ".join(unknown))
        return values

    @model_validator(mode="before")
    @classmethod
    def migrate_model_connection(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        try:
            official_base(value.get("model_api_base_url") or value.get("codex_model_base_url") or DEEPSEEK_BASE)
            official_model(value.get("model_name", UNIFIED_MODEL))
            if value.get("model_api_style", "anthropic") not in ("anthropic", "openai"):
                raise ValueError("unsupported legacy protocol")
        except (ValueError, TypeError, AttributeError):
            # Preserve the saved file, but never send another provider's secret
            # to DeepSeek when opening a legacy installation.
            value.update(model_api_base_url=DEEPSEEK_BASE, codex_model_base_url=DEEPSEEK_BASE,
                         model_name=UNIFIED_MODEL, llm_api_key="", llm_enabled=False,
                         job_analysis_enabled=False, codex_runtime_enabled=False,
                         model_connection_migration_required=True)
        value["model_api_style"] = "anthropic"
        return value

    @model_validator(mode="after")
    def unified_flash_model(self):
        self.model_provider_name = "DeepSeek"
        self.codex_model_provider_id = "deepseek"
        self.codex_model_api_key_env = "RECRUITOPS_LLM_API_KEY"
        base = official_base(self.model_api_base_url or self.codex_model_base_url)
        official_model(self.model_name)
        self.model_api_base_url = base
        self.codex_model_base_url = base
        self.llm_endpoint = base + "/anthropic/v1/messages"
        self.vision_endpoint = base + "/chat/completions"
        self.llm_model = self.codex_model = self.vision_model = self.model_name
        from packages.desktop_runtime.capabilities import saved_mail_configured

        mail_settings = {
            "mail_imap_host": self.mail_imap_host,
            "mail_imap_port": self.mail_imap_port,
            "mail_imap_username": self.mail_imap_username,
            "mail_imap_password": self.mail_imap_password,
        }
        if not saved_mail_configured(mail_settings):
            self.mail_enabled = False
            self.mail_sync_on_startup = False
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
    settings = Settings()
    overrides = {}
    local = settings.agent_root / ".data" / "settings" / "preferences.json"
    if local.is_file():
        from packages.user_settings import CONFIG_FIELDS
        overrides = json.loads(local.read_text(encoding="utf-8"))
        values = settings.model_dump()
        values.update({key: value for key, value in overrides.items() if key in CONFIG_FIELDS})
        # A complete saved official connection supersedes an obsolete .env
        # provider. Do not keep its migration flag and erase the new key later.
        if overrides.get("model_api_base_url") and overrides.get("llm_api_key"):
            try:
                official_base(overrides["model_api_base_url"])
                official_model(overrides.get("model_name", UNIFIED_MODEL))
                values["model_connection_migration_required"] = False
            except (ValueError, TypeError, AttributeError):
                pass
        if "model_api_base_url" not in overrides and overrides.get("codex_model_base_url"):
            values["model_api_base_url"] = overrides["codex_model_base_url"]
        settings = Settings.model_validate(values)
    if os.environ.get("RECRUITOPS_ENV") == "desktop-isolated":
        try:
            mask = json.loads(os.environ.get("RECRUITOPS_DESKTOP_CAPABILITIES", "{}"))
        except (ValueError, TypeError):
            mask = {}
        if not isinstance(mask, dict):
            mask = {}
        values = settings.model_dump()
        for key in DESKTOP_CAPABILITY_FIELDS:
            values[key] = bool(values[key] and mask.get(key) is True)
        instance_id = os.environ.get("RECRUITOPS_DESKTOP_INSTANCE_ID", "")
        # Mail is separately authorized below, never by a stale startup mask.
        values["mail_enabled"] = False
        values["mail_sync_on_startup"] = False
        if (os.environ.get("RECRUITOPS_DESKTOP_LAUNCH_MODE") == "packaged"
                and re.fullmatch(r"[0-9a-f]{32}", instance_id)
                and os.environ.get("RECRUITOPS_DESKTOP_WRITE_OPTIN") == instance_id
                and settings.write_enabled):
            from packages.desktop_runtime.capabilities import saved_mail_configured, saved_model_configured
            model_configured = saved_model_configured(overrides)
            values["llm_enabled"] = model_configured and overrides.get("llm_enabled") is True
            values["codex_runtime_enabled"] = (
                values["llm_enabled"]
                and overrides.get("codex_runtime_enabled") is True
            )
            values["job_analysis_enabled"] = (
                values["llm_enabled"] and mask.get("job_analysis_enabled") is True
            )
            values["automation_enabled"] = (
                values["codex_runtime_enabled"] and mask.get("automation_enabled") is True
            )
            values["mail_enabled"] = (
                saved_mail_configured(overrides) and mask.get("mail_enabled") is True
            )
            values["mail_sync_on_startup"] = (
                values["mail_enabled"]
                and overrides.get("mail_sync_on_startup") is True
                and mask.get("mail_sync_on_startup") is True
            )
        settings = Settings.model_validate(values)
    return settings
