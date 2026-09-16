from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .embeddings import DeterministicEmbeddingProvider, EmbeddingProvider
from .models import Citation, DocumentChunk, RetrievalResult


_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]|[^\W_]", re.IGNORECASE)


class Retriever(Protocol):
    """Replaceable retrieval boundary used by downstream RAG callers."""

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata_filter: Mapping[str, object] | None = None,
    ) -> list[RetrievalResult]:
        """Return ranked, citation-bearing chunks."""


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def lexical_score(query: str, document: str) -> float:
    """Measure query-token coverage in a document, normalized to ``[0, 1]``."""

    query_counts = Counter(_tokens(query))
    document_counts = Counter(_tokens(document))
    query_total = sum(query_counts.values())
    if query_total == 0:
        return 0.0
    overlap = sum(min(count, document_counts[token]) for token, count in query_counts.items())
    return overlap / query_total


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity and reject malformed vector dimensions."""

    if len(left) != len(right):
        raise ValueError("vectors must have the same dimension")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _validated_vector(vector: Sequence[float], dimension: int) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if len(values) != dimension:
        raise ValueError(f"embedding dimension must be {dimension}, got {len(values)}")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding values must be finite")
    return values


def _metadata_matches(metadata: Mapping[str, object], filters: Mapping[str, object]) -> bool:
    return all(metadata.get(key) == expected for key, expected in filters.items())


@dataclass(frozen=True)
class _IndexedChunk:
    chunk: DocumentChunk
    embedding: tuple[float, ...]


class LexicalCosineRetriever:
    """In-memory hybrid retriever with injectable embeddings and exact metadata filters."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        lexical_weight: float = 0.6,
        cosine_weight: float = 0.4,
    ):
        if lexical_weight < 0 or cosine_weight < 0 or lexical_weight + cosine_weight <= 0:
            raise ValueError("retrieval weights must be non-negative and not both zero")
        weight_total = lexical_weight + cosine_weight
        self.lexical_weight = lexical_weight / weight_total
        self.cosine_weight = cosine_weight / weight_total
        self.embedding_provider = embedding_provider or DeterministicEmbeddingProvider()
        if self.embedding_provider.dimension < 1:
            raise ValueError("embedding provider dimension must be positive")
        self._chunks: dict[str, _IndexedChunk] = {}

    def add(self, chunks: Iterable[DocumentChunk]) -> None:
        """Add or replace chunks by stable chunk ID."""

        for chunk in chunks:
            self.upsert(chunk)

    def add_documents(self, chunks: Iterable[DocumentChunk]) -> None:
        self.add(chunks)

    def upsert(self, chunk: DocumentChunk) -> None:
        embedding = _validated_vector(
            self.embedding_provider.embed(chunk.content),
            self.embedding_provider.dimension,
        )
        self._chunks[chunk.id] = _IndexedChunk(chunk=chunk, embedding=embedding)

    def upsert_document(self, chunk: DocumentChunk) -> None:
        self.upsert(chunk)

    def remove(self, chunk_id: str) -> None:
        self._chunks.pop(chunk_id, None)

    def clear(self) -> None:
        self._chunks.clear()

    def __len__(self) -> int:
        return len(self._chunks)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata_filter: Mapping[str, object] | None = None,
    ) -> list[RetrievalResult]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        query_embedding = _validated_vector(
            self.embedding_provider.embed(query),
            self.embedding_provider.dimension,
        )
        filters = metadata_filter or {}
        ranked: list[RetrievalResult] = []
        for indexed in self._chunks.values():
            chunk = indexed.chunk
            if not _metadata_matches(chunk.metadata, filters):
                continue
            lexical = lexical_score(query, chunk.content)
            cosine = cosine_similarity(query_embedding, indexed.embedding)
            combined = self.lexical_weight * lexical + self.cosine_weight * max(cosine, 0.0)
            ranked.append(
                RetrievalResult(
                    chunk=chunk,
                    score=combined,
                    lexical_score=lexical,
                    cosine_score=cosine,
                    citation=Citation(
                        chunk_id=chunk.id,
                        source=chunk.source,
                        source_ref=chunk.source_ref,
                        snippet=chunk.content,
                        metadata=dict(chunk.metadata),
                    ),
                )
            )

        ranked.sort(
            key=lambda result: (
                -result.score,
                -result.lexical_score,
                -result.cosine_score,
                result.chunk.id,
            )
        )
        return ranked[:top_k]


__all__ = [
    "LexicalCosineRetriever",
    "Retriever",
    "cosine_similarity",
    "lexical_score",
]
