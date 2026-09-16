from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
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
    """Call a self-hosted BGE-M3 or compatible embeddings endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        model: str = "BAAI/bge-m3",
        api_key: str | None = None,
        dimension: int = 1024,
        timeout: float = 20.0,
        query_prefix: str = "",
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("embedding endpoint must be HTTP(S)")
        if dimension < 2:
            raise ValueError("dimension must be at least 2")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.endpoint = endpoint
        self.model = model
        self.version = f"openai-compatible:{model}"
        if query_prefix:
            self.version += f":{dimension}:" + hashlib.sha256(query_prefix.encode()).hexdigest()[:12]
        self.api_key = api_key
        self.dimension = dimension
        self.timeout = timeout
        self.query_prefix = query_prefix

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed(self.query_prefix + text)

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), 16):
            vectors.extend(self._request(list(texts[start:start + 16])))
        return vectors

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("embedding service request failed") from exc
        try:
            rows = value["data"]
            if len(rows) != len(texts):
                raise ValueError("response count mismatch")
            # A single-vector legacy response may omit its index; batches may not.
            by_index = {int(row.get("index", 0 if len(rows) == 1 else -1)): row for row in rows}
            if set(by_index) != set(range(len(texts))) or len(by_index) != len(rows):
                raise ValueError("response indices mismatch")
            vectors = [[float(item) for item in by_index[i]["embedding"]] for i in range(len(texts))]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("embedding service returned an invalid response") from exc
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ValueError(f"embedding service returned dimension {len(vector)}, expected {self.dimension}")
            if not all(math.isfinite(item) for item in vector) or not any(vector):
                raise ValueError("embedding service returned invalid values")
        return vectors


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "DeterministicEmbeddingProvider",
    "EmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
]
