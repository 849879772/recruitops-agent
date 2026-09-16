from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import DocumentChunk
from .retriever import LexicalCosineRetriever


@dataclass(frozen=True)
class SourceDocument:
    source: str
    source_ref: str
    content: str
    metadata: dict[str, Any]


def split_document(text: str, *, max_chars: int = 900, overlap: int = 120) -> list[str]:
    """Split long JD/evidence text while keeping short heading blocks together."""

    if max_chars < 32 or overlap < 0 or overlap >= max_chars:
        raise ValueError("require 32 <= max_chars and 0 <= overlap < max_chars")
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if not normalized:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        tail = current[-overlap:] if overlap and current else ""
        current = f"{tail}\n\n{paragraph}".strip()
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars - overlap :].strip()
    if current:
        chunks.append(current)
    return chunks


def chunk_document(
    document: SourceDocument,
    *,
    max_chars: int = 900,
    overlap: int = 120,
) -> list[DocumentChunk]:
    """Create stable, citation-preserving chunks for one source document."""

    fingerprint = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
    chunks: list[DocumentChunk] = []
    for index, text in enumerate(
        split_document(document.content, max_chars=max_chars, overlap=overlap)
    ):
        chunk_id = hashlib.sha256(
            f"{document.source}:{document.source_ref}:{index}:{text}".encode("utf-8")
        ).hexdigest()
        chunks.append(
            DocumentChunk(
                id=chunk_id,
                content=text,
                source=document.source,
                source_ref=document.source_ref,
                metadata={
                    **document.metadata,
                    "chunk_index": index,
                    "document_fingerprint": fingerprint,
                },
            )
        )
    return chunks


def chunk_documents(
    documents: Iterable[SourceDocument],
    *,
    max_chars: int = 900,
    overlap: int = 120,
) -> list[DocumentChunk]:
    """Chunk approved documents in deterministic input order."""

    return [
        chunk
        for document in documents
        for chunk in chunk_document(document, max_chars=max_chars, overlap=overlap)
    ]


class IncrementalRagIndex:
    """In-memory index with stable IDs and fingerprint-based incremental updates."""

    def __init__(self, retriever: LexicalCosineRetriever | None = None):
        self.retriever = retriever or LexicalCosineRetriever()
        self._fingerprints: dict[str, str] = {}
        self._chunk_ids: dict[str, list[str]] = {}

    def upsert(self, document: SourceDocument) -> bool:
        fingerprint = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        if self._fingerprints.get(document.source_ref) == fingerprint:
            return False
        for chunk_id in self._chunk_ids.get(document.source_ref, []):
            self.retriever.remove(chunk_id)
        chunks = chunk_document(document)
        self.retriever.add(chunks)
        self._fingerprints[document.source_ref] = fingerprint
        self._chunk_ids[document.source_ref] = [chunk.id for chunk in chunks]
        return True

    def upsert_many(self, documents: Iterable[SourceDocument]) -> int:
        return sum(self.upsert(document) for document in documents)


__all__ = [
    "IncrementalRagIndex",
    "SourceDocument",
    "chunk_document",
    "chunk_documents",
    "split_document",
]
