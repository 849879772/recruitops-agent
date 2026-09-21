"""Local user preferences. Never include credentials in read responses."""
from pathlib import Path

DEFAULT_ON_CAPABILITY_FIELDS = frozenset({
    "job_analysis_enabled", "mail_enabled", "automation_enabled",
})

CONFIG_FIELDS = frozenset({
    "llm_enabled", "job_analysis_enabled", "codex_runtime_enabled", "model_api_base_url", "llm_api_key",
    "codex_model_provider_id", "model_provider_name", "model_api_style", "model_name",
    "offerbiu_industry_groups",
    "mail_enabled", "mail_imap_host", "mail_imap_port", "mail_imap_username",
    "mail_imap_password", "mail_imap_mailbox",
    "automation_enabled", "vision_enabled", "mail_sync_on_startup",
})
SECRET_FIELDS = frozenset({"llm_api_key", "mail_imap_password"})
LEGACY_MODEL_FIELDS = frozenset({"llm_endpoint", "llm_model", "codex_model_base_url", "codex_model", "vision_endpoint", "vision_model"})


def settings_dir(settings) -> Path:
    return Path(settings.agent_root) / ".data" / "settings"
