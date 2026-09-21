"""Startup-only owner configuration gates; never inherit ambient enable flags."""

import json
import ipaddress
import re
from urllib.parse import urlsplit

from packages.user_settings import DEFAULT_ON_CAPABILITY_FIELDS

from . import RuntimeFailure


CAPABILITY_FIELDS = (
    "llm_enabled", "job_analysis_enabled", "codex_runtime_enabled", "mail_enabled",
    "mail_sync_on_startup", "automation_enabled", "vision_enabled",
)


def saved_model_configured(settings):
    """Saved connection shape only, not provider or agent process readiness."""
    if not isinstance(settings, dict) or settings.get("model_api_style") not in ("anthropic", "openai"):
        return False
    if not all(isinstance(settings.get(name), str) and settings[name].strip()
               for name in ("llm_api_key", "model_name", "model_api_base_url")):
        return False
    try:
        parsed = urlsplit(settings["model_api_base_url"].strip())
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        return bool((parsed.scheme == "https" or local_http) and parsed.hostname
                    and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    except ValueError:
        return False


def saved_mail_configured(settings):
    """Validate saved IMAP connection shape without contacting a mailbox."""
    if not isinstance(settings, dict):
        return False
    if not all(isinstance(settings.get(name), str) and settings[name].strip()
               for name in ("mail_imap_host", "mail_imap_username", "mail_imap_password")):
        return False
    port = settings.get("mail_imap_port")
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    host = settings["mail_imap_host"]
    if any(character.isspace() for character in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").removesuffix(".")
    except UnicodeError:
        return False
    if not host or len(host) > 253 or re.fullmatch(r"[0-9.]+", host):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
               for label in host.split("."))


def automation_hot_reload_allowed(root, instance_id, *, writes=False):
    """Allow safe automation reload with the default-on flag and existing gates."""
    if not writes:
        return False
    preferences = root / ".data/settings/preferences.json"
    if not preferences.is_file():
        return False
    try:
        settings = json.loads(preferences.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if (not isinstance(settings, dict)
            or ("automation_enabled" in settings
                and type(settings["automation_enabled"]) is not bool)):
        return False
    if (settings.get("llm_enabled") is not True
            or settings.get("codex_runtime_enabled") is not True
            or not saved_model_configured(settings)):
        return False
    marker = root / "config/runtime-capabilities.json"
    if not marker.is_file():
        return True
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(
        isinstance(record, dict)
        and record.get("schema") == 1
        and record.get("instance_id") == instance_id
        and type(record.get("first_run_complete")) is bool
    )


def configured_capabilities(root, instance_id, *, writes=False):
    result = dict.fromkeys(CAPABILITY_FIELDS, False)
    marker = root / "config/runtime-capabilities.json"
    preferences = root / ".data/settings/preferences.json"
    if not writes or not preferences.is_file():
        return result
    try:
        settings = json.loads(preferences.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError()
        completed = False
        if marker.is_file():
            record = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("schema") != 1
                    or record.get("instance_id") != instance_id
                    or type(record.get("first_run_complete")) is not bool):
                raise ValueError()
            completed = record["first_run_complete"]
        for name in result:
            if name in settings and not isinstance(settings[name], bool):
                raise ValueError()
            requested = name in DEFAULT_ON_CAPABILITY_FIELDS or settings.get(name, False)
            result[name] = requested and (
                completed
                or name in {"llm_enabled", "codex_runtime_enabled", "mail_sync_on_startup"}
                or name in DEFAULT_ON_CAPABILITY_FIELDS
            )
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeFailure("invalid_instance_capabilities") from exc
    model_configured = saved_model_configured(settings)
    result["llm_enabled"] &= model_configured
    for name in ("job_analysis_enabled", "codex_runtime_enabled", "vision_enabled"):
        result[name] &= result["llm_enabled"]
    result["automation_enabled"] &= result["codex_runtime_enabled"]
    result["mail_enabled"] &= saved_mail_configured(settings)
    result["mail_sync_on_startup"] &= result["mail_enabled"]
    return result
