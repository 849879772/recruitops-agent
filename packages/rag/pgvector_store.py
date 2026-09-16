from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Mapping

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    bindparam,
    delete,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Engine

from .embeddings import DeterministicEmbeddingProvider, EmbeddingProvider
from .models import Citation, DocumentChunk, RetrievalResult
from .persistent_index import DocumentSyncStats


metadata = MetaData()
document_chunks = Table(
    "document_chunks",
    metadata,
    # The migration owns production indexes; this table mirrors its columns
    # for typed SQLAlchemy access and upserts.
    Column("id", String(255), primary_key=True),
    Column("content", Text, nullable=False),
    Column("source", String(128), nullable=False),
    Column("source_ref", String(2048), nullable=False),
    Column("chunk_index", Integer, nullable=False, default=0),
    Column("metadata", JSONB, nullable=False, default=dict),
    Column("content_hash", String(64), nullable=False),
    Column("embedding_model", String(128), nullable=False),
    Column("embedding", Vector(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source", "source_ref", "chunk_index"),
)


class PgVectorDocumentStore:
    """Persist and query citation-preserving chunks in Agent-owned pgvector."""

    def __init__(
        self,
        engine: Engine,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.engine = engine
        self.embedding_provider = embedding_provider or DeterministicEmbeddingProvider()
        if self.embedding_provider.dimension != 64:
            raise ValueError("the current document_chunks migration requires 64 dimensions")

    def ensure_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        metadata.create_all(self.engine, tables=[document_chunks])

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
        statement = insert(document_chunks).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[document_chunks.c.id],
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
        """Replace one document atomically and embed only changed chunks."""

        if any(chunk.source != source or chunk.source_ref != source_ref for chunk in chunks):
            raise ValueError("all chunks must belong to the synchronized source document")
        model = getattr(self.embedding_provider, "version", "custom")
        with self.engine.begin() as connection:
            existing_rows = connection.execute(
                select(
                    document_chunks.c.id,
                    document_chunks.c.content_hash,
                    document_chunks.c.embedding_model,
                    document_chunks.c.chunk_index,
                    document_chunks.c.metadata,
                ).where(
                    document_chunks.c.source == source,
                    document_chunks.c.source_ref == source_ref,
                )
            ).mappings()
            existing = {row["id"]: row for row in existing_rows}
            changed = 0
            unchanged = 0
            incoming_chunk_ids = {chunk.id for chunk in chunks}
            stale_ids = set(existing) - incoming_chunk_ids
            if stale_ids:
                connection.execute(
                    delete(document_chunks).where(document_chunks.c.id.in_(stale_ids))
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
                statement = insert(document_chunks).values(**values)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[document_chunks.c.id],
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
                statement = delete(document_chunks).where(document_chunks.c.source == source)
                if keep[source]:
                    statement = statement.where(~document_chunks.c.source_ref.in_(keep[source]))
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
        distance = document_chunks.c.embedding.cosine_distance(
            bindparam("query_embedding", value=query_vector, type_=Vector(64))
        ).label("distance")
        statement = select(document_chunks, distance).order_by(distance).limit(top_k)
        if metadata_filter:
            statement = statement.where(document_chunks.c.metadata.contains(dict(metadata_filter)))
        with self.engine.connect() as connection:
            # The production index currently has 100 IVFFlat lists. Searching all
            # lists prevents zero-recall on the deliberately small knowledge base.
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


__all__ = ["PgVectorDocumentStore", "document_chunks", "metadata"]
