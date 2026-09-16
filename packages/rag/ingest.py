from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from .indexing import SourceDocument


_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|cookie)\s*[:=]\s*[^\s,;]+"
)
_SENSITIVE_JSON_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|cookie|authorization)"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _redact_secret(match: re.Match[str]) -> str:
    key = re.split(r"\s*[:=]\s*", match.group(0), maxsplit=1)[0]
    return f"{key}=[REDACTED]"


def clean_source_text(text: str, *, max_chars: int = 100_000) -> str:
    """Normalize imported evidence and redact obvious credential-shaped values."""

    normalized = text.replace("\ufeff", "")
    normalized = _CONTROL_RE.sub(" ", normalized)
    normalized = _SECRET_RE.sub(_redact_secret, normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized[:max_chars]


def documents_from_text_files(
    root: Path,
    *,
    source: str,
    metadata: Mapping[str, Any] | None = None,
    suffixes: Iterable[str] = (".md", ".txt"),
) -> list[SourceDocument]:
    """Build deterministic documents from approved local evidence directories."""

    allowed = {suffix.casefold() for suffix in suffixes}
    base_metadata = dict(metadata or {})
    documents: list[SourceDocument] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in allowed:
            continue
        content = clean_source_text(path.read_text(encoding="utf-8", errors="replace"))
        if content:
            documents.append(
                SourceDocument(
                    source=source,
                    source_ref=path.as_posix(),
                    content=content,
                    metadata={**base_metadata, "path": path.as_posix()},
                )
            )
    return documents


def documents_from_json_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source: str,
    source_ref_field: str = "id",
    text_fields: tuple[str, ...] = ("title", "content", "description", "jd_raw"),
    metadata: Mapping[str, Any] | None = None,
) -> list[SourceDocument]:
    """Convert JSON report records to citation-preserving documents without secrets."""

    base_metadata = dict(metadata or {})
    documents: list[SourceDocument] = []
    for index, record in enumerate(records):
        parts = [str(record[field]) for field in text_fields if record.get(field)]
        content = clean_source_text("\n".join(parts))
        if not content:
            continue
        source_ref = str(record.get(source_ref_field) or f"record-{index}")
        documents.append(
            SourceDocument(
                source=source,
                source_ref=source_ref,
                content=content,
                metadata={**base_metadata, "record_index": index},
            )
        )
    return documents


def _redact_structured_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_JSON_KEY_RE.search(str(key))
                else _redact_structured_value(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_structured_value(item) for item in value]
    return value


def documents_from_structured_json_file(
    path: Path,
    *,
    source: str,
    metadata: Mapping[str, Any] | None = None,
    source_ref_field: str | None = None,
) -> list[SourceDocument]:
    """Convert a list or keyed mapping JSON file into redacted evidence documents."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"JSON source is not a readable file: {resolved}")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON source is invalid: {resolved}") from exc

    records: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            identity = item.get(source_ref_field) if source_ref_field else None
            records.append((str(identity or f"record-{index}"), item))
    elif isinstance(value, Mapping):
        if value and all(isinstance(item, Mapping) for item in value.values()):
            records.extend((str(key), item) for key, item in value.items())
        else:
            records.append(("document", value))
    else:
        raise ValueError(f"JSON source must contain a list or object: {resolved}")

    base_metadata = dict(metadata or {})
    documents: list[SourceDocument] = []
    for index, (identity, record) in enumerate(records):
        redacted = _redact_structured_value(record)
        content = clean_source_text(
            json.dumps(redacted, ensure_ascii=False, sort_keys=True, indent=2)
        )
        if not content:
            continue
        documents.append(
            SourceDocument(
                source=source,
                source_ref=f"{resolved.as_posix()}#{identity}",
                content=content,
                metadata={
                    **base_metadata,
                    "path": resolved.as_posix(),
                    "record_index": index,
                    "record_key": identity,
                },
            )
        )
    return documents


def load_json_records(path: Path) -> list[dict[str, Any]]:
    """Read a JSON list while rejecting non-list or malformed evidence files."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON list: {path}")
    return [item for item in value if isinstance(item, dict)]


def document_from_profile_config(path: Path) -> SourceDocument:
    """Read only the allowlisted candidate profile section from a YAML config."""

    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile = value.get("profile") if isinstance(value, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f"profile section is missing: {path}")
    allowlisted: dict[str, Any] = {
        key: profile[key]
        for key in ("skills", "direction", "degree", "job_type")
        if key in profile
    }
    matching = profile.get("matching")
    if isinstance(matching, dict):
        verified_matching = {
            key: matching[key]
            for key in (
                "direction_policy",
                "primary_directions",
                "secondary_directions",
                "project_evidence",
                "supporting_skills",
            )
            if key in matching
        }
        if verified_matching:
            allowlisted["matching"] = verified_matching
    content = clean_source_text(
        yaml.safe_dump(allowlisted, allow_unicode=True, sort_keys=False)
    )
    if not content:
        raise ValueError(f"profile section has no approved evidence fields: {path}")
    resolved = path.resolve()
    return SourceDocument(
        source="candidate_profile",
        source_ref=resolved.as_posix(),
        content=content,
        metadata={
            "domain": "candidate",
            "kind": "profile",
            "trust": "user_configured",
            "path": resolved.as_posix(),
        },
    )


__all__ = [
    "clean_source_text",
    "documents_from_json_records",
    "documents_from_structured_json_file",
    "documents_from_text_files",
    "document_from_profile_config",
    "load_json_records",
]
