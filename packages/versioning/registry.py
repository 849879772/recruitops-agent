from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactKind(StrEnum):
    PROMPT = "prompt"
    TOOL_PROTOCOL = "tool_protocol"
    KNOWLEDGE_SCHEMA = "knowledge_schema"
    EVAL_FIXTURE = "eval_fixture"


class ArtifactVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    kind: ArtifactKind
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1)


class VersionConflictError(ValueError):
    pass


class VersionRegistry:
    """Deterministic manifest for prompts, protocols, schemas, and frozen evals."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ArtifactVersion] = {}

    def register(
        self,
        *,
        name: str,
        kind: ArtifactKind,
        version: str,
        content: str | bytes | dict[str, Any] | list[Any],
        source_ref: str,
    ) -> ArtifactVersion:
        raw = self._canonical(content)
        item = ArtifactVersion(
            name=name,
            kind=kind,
            version=version,
            sha256=sha256(raw).hexdigest(),
            source_ref=source_ref,
        )
        key = (name, version)
        existing = self._items.get(key)
        if existing is not None and existing.sha256 != item.sha256:
            raise VersionConflictError(
                f"artifact {name!r} version {version!r} already has different content"
            )
        self._items[key] = item
        return item

    def list(self) -> list[ArtifactVersion]:
        return sorted(self._items.values(), key=lambda item: (item.kind.value, item.name, item.version))

    def export(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.model_dump(mode="json") for item in self.list()]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _canonical(content: str | bytes | dict[str, Any] | list[Any]) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


__all__ = ["ArtifactKind", "ArtifactVersion", "VersionConflictError", "VersionRegistry"]
