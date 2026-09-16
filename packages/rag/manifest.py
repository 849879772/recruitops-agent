"""Versioned, local-first RAG source manifests and safe previews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .indexing import SourceDocument, chunk_document
from .ingest import (
    clean_source_text,
    document_from_profile_config,
    documents_from_structured_json_file,
    documents_from_text_files,
)
from .lujie_resume import documents_from_lujie_sqlite


_DEFAULT_SOURCES = {
    "text_file": "text_file",
    "text_root": "text_root",
    "profile_config": "candidate_profile",
    "json_file": "structured_evidence",
    "lujie_resume": "candidate_evidence",
}


class ManifestSource(BaseModel):
    """One explicitly approved local source in a versioned manifest."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    kind: Literal[
        "text_file",
        "text_root",
        "profile_config",
        "json_file",
        "lujie_resume",
    ]
    path: str = Field(
        min_length=1,
        validation_alias=AliasChoices("path", "file"),
    )
    source: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    record_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_record_selection(self) -> "ManifestSource":
        if self.kind == "lujie_resume" and not self.record_ids:
            raise ValueError("lujie_resume requires explicit record_ids")
        if self.kind != "lujie_resume" and self.record_ids:
            raise ValueError("record_ids are only valid for lujie_resume")
        if len(self.record_ids) != len(set(self.record_ids)):
            raise ValueError("record_ids must not contain duplicates")
        return self


class RagManifest(BaseModel):
    """The supported version-1 RAG source manifest."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal[1]
    sources: list[ManifestSource]
    managed_sources: list[str] = Field(default_factory=list)

    @field_validator("managed_sources")
    @classmethod
    def _validate_managed_sources(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("managed_sources must contain non-empty source names")
        if len(values) != len(set(values)):
            raise ValueError("managed_sources must not contain duplicates")
        return values


def _resolved_manifest_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"manifest is not a readable file: {resolved}")
    return resolved


def load_manifest(path: Path) -> RagManifest:
    """Load and validate one version-1 YAML manifest."""

    manifest_path = _resolved_manifest_path(path)
    try:
        value = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"manifest is not readable: {manifest_path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"manifest is not valid YAML: {manifest_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("manifest root must be a mapping")
    return RagManifest.model_validate(value)


def _source_path(manifest_path: Path, source: ManifestSource) -> Path:
    candidate = Path(source.path).expanduser()
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    return candidate.resolve()


def _file_document(
    path: Path,
    *,
    source: str,
    metadata: dict[str, Any],
) -> SourceDocument | None:
    if not path.is_file():
        raise ValueError(f"text file is not a readable file: {path}")
    try:
        content = clean_source_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise ValueError(f"text file is not readable: {path}") from exc
    if not content:
        return None
    return SourceDocument(
        source=source,
        source_ref=path.as_posix(),
        content=content,
        metadata={**metadata, "path": path.as_posix()},
    )


def validate_unique_documents(
    documents: Iterable[SourceDocument],
) -> list[SourceDocument]:
    """Reject duplicate source/source_ref pairs before any store is touched."""

    materialized = list(documents)
    seen: set[tuple[str, str]] = set()
    for document in materialized:
        key = (document.source, document.source_ref)
        if key in seen:
            raise ValueError(
                "duplicate source+source_ref: "
                f"{document.source} / {document.source_ref}"
            )
        seen.add(key)
    return materialized


def documents_from_manifest(
    path: Path,
    manifest: RagManifest | None = None,
) -> list[SourceDocument]:
    """Expand a manifest, resolving every entry relative to the manifest file."""

    manifest_path = _resolved_manifest_path(path)
    manifest = manifest or load_manifest(manifest_path)
    documents: list[SourceDocument] = []
    for source_spec in manifest.sources:
        source_path = _source_path(manifest_path, source_spec)
        source_name = source_spec.source or _DEFAULT_SOURCES[source_spec.kind]
        if source_spec.kind == "text_file":
            document = _file_document(
                source_path,
                source=source_name,
                metadata=dict(source_spec.metadata),
            )
            if document is not None:
                documents.append(document)
        elif source_spec.kind == "text_root":
            if not source_path.is_dir():
                raise ValueError(f"text root is not a readable directory: {source_path}")
            documents.extend(
                documents_from_text_files(
                    source_path,
                    source=source_name,
                    metadata=dict(source_spec.metadata),
                )
            )
        elif source_spec.kind == "profile_config":
            if not source_path.is_file():
                raise ValueError(f"profile config is not a readable file: {source_path}")
            profile_document = document_from_profile_config(source_path)
            documents.append(
                SourceDocument(
                    source=source_name,
                    source_ref=profile_document.source_ref,
                    content=profile_document.content,
                    metadata={
                        **profile_document.metadata,
                        **dict(source_spec.metadata),
                        "path": source_path.as_posix(),
                    },
                )
            )
        elif source_spec.kind == "json_file":
            documents.extend(
                documents_from_structured_json_file(
                    source_path,
                    source=source_name,
                    metadata=dict(source_spec.metadata),
                )
            )
        else:
            resume_documents = documents_from_lujie_sqlite(
                source_path,
                approved_record_ids=source_spec.record_ids,
            )
            documents.extend(
                SourceDocument(
                    source=source_name,
                    source_ref=document.source_ref,
                    content=document.content,
                    metadata={**document.metadata, **dict(source_spec.metadata)},
                )
                for document in resume_documents
            )
    return validate_unique_documents(documents)


def load_manifest_documents(path: Path) -> tuple[RagManifest, list[SourceDocument]]:
    """Load a manifest and its validated source documents together."""

    manifest_path = _resolved_manifest_path(path)
    manifest = load_manifest(manifest_path)
    return manifest, documents_from_manifest(manifest_path, manifest)


def preview_documents(documents: Iterable[SourceDocument]) -> dict[str, Any]:
    """Return counts and fingerprints without exposing document contents."""

    summaries: list[dict[str, Any]] = []
    for document in validate_unique_documents(documents):
        fingerprint = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        summaries.append(
            {
                "source": document.source,
                "source_ref": document.source_ref,
                "documents": 1,
                "chunks": len(chunk_document(document)),
                "characters": len(document.content),
                "content_fingerprint": fingerprint,
            }
        )

    aggregate_input = [
        {
            "source": summary["source"],
            "source_ref": summary["source_ref"],
            "content_fingerprint": summary["content_fingerprint"],
        }
        for summary in summaries
    ]
    aggregate_fingerprint = hashlib.sha256(
        json.dumps(aggregate_input, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "mode": "dry-run",
        "documents": len(summaries),
        "chunks": sum(summary["chunks"] for summary in summaries),
        "characters": sum(summary["characters"] for summary in summaries),
        "content_fingerprint": aggregate_fingerprint,
        "source_summaries": summaries,
    }


__all__ = [
    "ManifestSource",
    "RagManifest",
    "documents_from_manifest",
    "load_manifest",
    "load_manifest_documents",
    "preview_documents",
    "validate_unique_documents",
]
