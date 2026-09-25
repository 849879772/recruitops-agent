from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol


DEFAULT_EMBEDDING_DIMENSION = 64
QWEN_QUERY_PREFIX = "Instruct: Given a question about personal documents, retrieve relevant passages that provide evidence to answer it.\nQuery: "
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]|[^\W_]", re.IGNORECASE)


class EmbeddingProvider(Protocol):
    """Small provider contract so a hosted or model-backed embedder can be swapped in later."""

    dimension: int

    def embed(self, text: str) -> Sequence[float]:
        """Return one vector for ``text`` without mutating external state."""


def _features(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.casefold())
    features: list[str] = []
    for token in tokens:
        features.append(f"token:{token}")
        if len(token) > 2:
            features.extend(
                f"gram:{token[index:index + 3]}" for index in range(len(token) - 2)
            )
    return features


class DeterministicEmbeddingProvider:
    """A stable, dependency-free embedding for local tests and offline development.

    This is feature hashing, not a semantic model. It deliberately makes no network
    or model API calls; a production provider can implement ``EmbeddingProvider``
    without changing the retriever contract.
    """

    version = "deterministic-local-v1"

    def __init__(self, dimension: int = DEFAULT_EMBEDDING_DIMENSION):
        if dimension < 2:
            raise ValueError("dimension must be at least 2")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature in _features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, byteorder="big") % self.dimension
            vector[bucket] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class OpenAICompatibleEmbeddingProvider:
    """Retired import shim: old integrations fail locally, never transmit data."""

    def __init__(self, *args, **kwargs):
        raise ValueError("Remote compatible embeddings are retired; use the local index")


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DeterministicEmbeddingProvider",
    "EmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
]
