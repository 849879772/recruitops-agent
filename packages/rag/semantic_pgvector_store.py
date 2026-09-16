from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Mapping

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Engine

from .embeddings import EmbeddingProvider
from .models import Citation, DocumentChunk, RetrievalResult
from .persistent_index import DocumentSyncStats


semantic_metadata = MetaData()
semantic_document_chunks = Table(
    "semantic_document_chunks",
    semantic_metadata,
    Column("id", String(255), primary_key=True),
    Column("content", String, nullable=False),
    Column("source", String(128), nullable=False),
    Column("source_ref", String(2048), nullable=False),
    Column("chunk_index", Integer, nullable=False, default=0),
    Column("metadata", JSONB, nullable=False, default=dict),
    Column("content_hash", String(64), nullable=False),
    Column("embedding_model", String(128), nullable=False),
    Column("embedding", Vector(1024), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class SemanticPgVectorDocumentStore:
    """BGE-M3-sized pgvector store, separate from the 64-dim offline table."""

    def __init__(self, engine: Engine, embedding_provider: EmbeddingProvider):
        if embedding_provider.dimension != 1024:
            raise ValueError("semantic_document_chunks requires 1024 dimensions")
        self.engine = engine
        self.embedding_provider = embedding_provider

    def ensure_schema(self) -> None:
        semantic_metadata.create_all(self.engine, tables=[semantic_document_chunks])

    def upsert(self, chunk: DocumentChunk) -> None:
        now = datetime.now(timezone.utc)
        values = {
            "id": chunk.id,
            "content": chunk.content,
            "source": chunk.source,
            "source_ref": chunk.source_ref,
            "chunk_index": int(chunk.metadata.get("chunk_index", 0)),
            "metadata": dict(chunk.metadata),
            "content_hash": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
            "embedding_model": getattr(self.embedding_provider, "version", "custom"),
            "embedding": list(self.embedding_provider.embed(chunk.content)),
            "created_at": now,
            "updated_at": now,
        }
        statement = insert(semantic_document_chunks).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[semantic_document_chunks.c.id],
            set_={key: statement.excluded[key] for key in values if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def sync_chunks(
        self,
        *,
        source: str,
        source_ref: str,
        chunks: list[DocumentChunk],
    ) -> DocumentSyncStats:
        """Replace one semantic document atomically without re-embedding unchanged chunks."""

        if any(chunk.source != source or chunk.source_ref != source_ref for chunk in chunks):
            raise ValueError("all chunks must belong to the synchronized source document")
        model = getattr(self.embedding_provider, "version", "custom")
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(
                    semantic_document_chunks.c.id,
                    semantic_document_chunks.c.content_hash,
                    semantic_document_chunks.c.embedding_model,
                    semantic_document_chunks.c.chunk_index,
                    semantic_document_chunks.c.metadata,
                ).where(
                    semantic_document_chunks.c.source == source,
                    semantic_document_chunks.c.source_ref == source_ref,
                )
            ).mappings()
            existing = {row["id"]: row for row in rows}
            changed = 0
            unchanged = 0
            incoming_chunk_ids = {chunk.id for chunk in chunks}
            stale_ids = set(existing) - incoming_chunk_ids
            if stale_ids:
                connection.execute(
                    delete(semantic_document_chunks).where(
                        semantic_document_chunks.c.id.in_(stale_ids)
                    )
                )
                existing = {
                    chunk_id: row
                    for chunk_id, row in existing.items()
                    if chunk_id not in stale_ids
                }
            now = datetime.now(timezone.utc)
            for chunk in chunks:
                content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                current = existing.get(chunk.id)
                chunk_metadata = dict(chunk.metadata)
                if (
                    current
                    and current["content_hash"] == content_hash
                    and current["embedding_model"] == model
                    and (current["metadata"] or {}) == chunk_metadata
                ):
                    unchanged += 1
                    continue
                values = {
                    "id": chunk.id,
                    "content": chunk.content,
                    "source": chunk.source,
                    "source_ref": chunk.source_ref,
                    "chunk_index": int(chunk.metadata.get("chunk_index", 0)),
                    "metadata": chunk_metadata,
                    "content_hash": content_hash,
                    "embedding_model": model,
                    "embedding": list(self.embedding_provider.embed(chunk.content)),
                    "created_at": now,
                    "updated_at": now,
                }
                statement = insert(semantic_document_chunks).values(**values)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[semantic_document_chunks.c.id],
                        set_={
                            key: statement.excluded[key]
                            for key in values
                            if key not in {"id", "created_at"}
                        },
                    )
                )
                changed += 1
        return DocumentSyncStats(
            source=source,
            source_ref=source_ref,
            changed_chunks=changed,
            unchanged_chunks=unchanged,
            deleted_chunks=len(stale_ids),
        )

    def prune_managed_sources(
        self,
        *,
        managed_sources: Iterable[str],
        keep_source_refs: Mapping[str, Iterable[str]],
    ) -> int:
        """Delete only stale chunks inside explicitly managed sources."""

        sources = list(managed_sources)
        if not sources or any(not source for source in sources):
            raise ValueError("managed_sources must not be empty")
        if len(sources) != len(set(sources)):
            raise ValueError("managed_sources must not contain duplicates")
        keep = {
            source: {source_ref for source_ref in keep_source_refs.get(source, ())}
            for source in sources
        }
        if not any(keep.values()):
            raise ValueError("cannot prune without documents")

        deleted_chunks = 0
        with self.engine.begin() as connection:
            for source in sources:
                statement = delete(semantic_document_chunks).where(
                    semantic_document_chunks.c.source == source
                )
                if keep[source]:
                    statement = statement.where(
                        ~semantic_document_chunks.c.source_ref.in_(keep[source])
                    )
                result = connection.execute(statement)
                deleted_chunks += int(result.rowcount or 0)
        return deleted_chunks

    def prune_managed(
        self,
        *,
        managed_sources: Iterable[str],
        keep_source_refs: Mapping[str, Iterable[str]],
    ) -> int:
        """Compatibility alias for the controlled managed-source prune."""

        return self.prune_managed_sources(
            managed_sources=managed_sources,
            keep_source_refs=keep_source_refs,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata_filter: Mapping[str, object] | None = None,
    ) -> list[RetrievalResult]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        query_vector = list(self.embedding_provider.embed(query))
        distance = semantic_document_chunks.c.embedding.cosine_distance(
            bindparam("query_embedding", value=query_vector, type_=Vector(1024))
        ).label("distance")
        statement = select(semantic_document_chunks, distance).order_by(distance).limit(top_k)
        if metadata_filter:
            statement = statement.where(semantic_document_chunks.c.metadata.contains(dict(metadata_filter)))
        with self.engine.connect() as connection:
            connection.execute(text("SET LOCAL ivfflat.probes = 100"))
            rows = connection.execute(statement).mappings().all()
        results: list[RetrievalResult] = []
        for row in rows:
            chunk = DocumentChunk(
                id=row["id"],
                content=row["content"],
                source=row["source"],
                source_ref=row["source_ref"],
                metadata=row["metadata"] or {},
            )
            score = max(0.0, 1.0 - float(row["distance"]))
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    score=score,
                    lexical_score=0.0,
                    cosine_score=score,
                    citation=Citation(
                        chunk_id=chunk.id,
                        source=chunk.source,
                        source_ref=chunk.source_ref,
                        snippet=chunk.content,
                        metadata=dict(chunk.metadata),
                    ),
                )
            )
        return results


__all__ = ["SemanticPgVectorDocumentStore", "semantic_document_chunks", "semantic_metadata"]
