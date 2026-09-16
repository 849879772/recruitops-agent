from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from packages.rag import (
    DEFAULT_EMBEDDING_DIMENSION,
    DeterministicEmbeddingProvider,
    DocumentChunk,
    EmbeddingProvider,
    LexicalCosineRetriever,
    cosine_similarity,
)


def test_document_chunk_preserves_source_and_metadata_for_citations() -> None:
    chunk = DocumentChunk(
        chunk_id="chunk-1",
        text="Python backend experience",
        source="resume",
        source_ref="resume://projects/1",
        metadata={"kind": "project", "year": 2026},
    )

    assert chunk.id == "chunk-1"
    assert chunk.content == chunk.text
    assert chunk.source_ref == "resume://projects/1"
    assert chunk.metadata["kind"] == "project"


def test_deterministic_embedding_is_local_and_repeatable() -> None:
    provider = DeterministicEmbeddingProvider()

    first = provider.embed("Python backend")
    second = provider.embed("Python backend")

    assert provider.dimension == DEFAULT_EMBEDDING_DIMENSION
    assert first == second
    assert len(first) == DEFAULT_EMBEDDING_DIMENSION
    assert cosine_similarity(first, first) == pytest.approx(1.0)
    assert provider.embed("different text") != first


class _FixedProvider:
    dimension = 3

    def embed(self, text: str) -> Sequence[float]:
        if "python" in text.casefold():
            return (1.0, 0.0, 0.0)
        if "crawler" in text.casefold():
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


def test_retriever_supports_replaceable_provider_filter_top_k_and_citation() -> None:
    provider: EmbeddingProvider = _FixedProvider()
    retriever = LexicalCosineRetriever(provider)
    retriever.add_documents(
        [
            DocumentChunk(
                id="chunk-python",
                content="Python backend API",
                source="resume",
                source_ref="resume://projects/python",
                metadata={"kind": "project"},
            ),
            DocumentChunk(
                id="chunk-crawler",
                content="Crawler pagination failure",
                source="crawler-report",
                source_ref="report://crawler/1",
                metadata={"kind": "crawler"},
            ),
        ]
    )

    results = retriever.search(
        "Python",
        top_k=1,
        metadata_filter={"kind": "project"},
    )

    assert len(results) == 1
    result = results[0]
    assert result.chunk.id == "chunk-python"
    assert result.lexical_score == pytest.approx(1.0)
    assert result.cosine_score == pytest.approx(1.0)
    assert result.score == pytest.approx(1.0)
    assert result.citation.source == "resume"
    assert result.citation.source_ref == "resume://projects/python"
    assert result.citation.chunk_id == result.chunk.id


def test_retriever_upserts_and_rejects_invalid_top_k() -> None:
    retriever = LexicalCosineRetriever()
    chunk = DocumentChunk(
        id="chunk-1",
        content="first version",
        source="fixture",
        source_ref="fixture://1",
    )
    retriever.upsert(chunk)
    retriever.upsert(chunk.model_copy(update={"content": "updated version"}))

    assert len(retriever) == 1
    assert retriever.search("updated")[0].chunk.content == "updated version"
    with pytest.raises(ValueError, match="top_k"):
        retriever.search("updated", top_k=0)


def test_pgvector_migration_declares_chunk_contract() -> None:
    migration = Path(__file__).parents[1] / "migrations" / "002_rag.sql"
    sql = migration.read_text(encoding="utf-8").lower()

    assert "create extension if not exists vector" in sql
    assert "document_chunks" in sql
    assert "source_ref" in sql
    assert "metadata jsonb" in sql
    assert f"vector({DEFAULT_EMBEDDING_DIMENSION})" in sql
    assert "vector_cosine_ops" in sql
