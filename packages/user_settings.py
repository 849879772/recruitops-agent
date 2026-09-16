"""Local user preferences. Never include credentials in read responses."""
from pathlib import Path

CONFIG_FIELDS = frozenset({
    "llm_enabled", "job_analysis_enabled", "codex_runtime_enabled", "model_api_base_url", "llm_api_key",
    "mail_enabled", "mail_imap_host", "mail_imap_port", "mail_imap_username",
    "mail_imap_password", "mail_imap_mailbox",
})
SECRET_FIELDS = frozenset({"llm_api_key", "mail_imap_password"})
LEGACY_MODEL_FIELDS = frozenset({"llm_endpoint", "llm_model", "codex_model_base_url", "codex_model", "vision_endpoint", "vision_model"})


def settings_dir(settings) -> Path:
    return Path(settings.agent_root) / ".data" / "settings"
