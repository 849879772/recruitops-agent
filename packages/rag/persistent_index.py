from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .indexing import SourceDocument, chunk_document
from .models import DocumentChunk


@dataclass(frozen=True)
class DocumentSyncStats:
    source: str
    source_ref: str
    changed_chunks: int = 0
    unchanged_chunks: int = 0
    deleted_chunks: int = 0


@dataclass(frozen=True)
class RagSyncStats:
    documents: int = 0
    changed_chunks: int = 0
    unchanged_chunks: int = 0
    deleted_chunks: int = 0


class SyncableDocumentStore(Protocol):
    def sync_chunks(
        self,
        *,
        source: str,
        source_ref: str,
        chunks: list[DocumentChunk],
    ) -> DocumentSyncStats: ...


class PersistentRagIndexer:
    """Synchronize approved documents without re-embedding unchanged chunks."""

    def __init__(self, store: SyncableDocumentStore):
        self.store = store

    def sync(self, document: SourceDocument) -> DocumentSyncStats:
        return self.store.sync_chunks(
            source=document.source,
            source_ref=document.source_ref,
            chunks=chunk_document(document),
        )

    def sync_many(self, documents: Iterable[SourceDocument]) -> RagSyncStats:
        total = RagSyncStats()
        for document in documents:
            result = self.sync(document)
            total = RagSyncStats(
                documents=total.documents + 1,
                changed_chunks=total.changed_chunks + result.changed_chunks,
                unchanged_chunks=total.unchanged_chunks + result.unchanged_chunks,
                deleted_chunks=total.deleted_chunks + result.deleted_chunks,
            )
        return total


__all__ = [
    "DocumentSyncStats",
    "PersistentRagIndexer",
    "RagSyncStats",
    "SyncableDocumentStore",
]
