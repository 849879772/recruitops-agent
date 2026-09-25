import json

import pytest

from packages.rag import OpenAICompatibleEmbeddingProvider, SemanticPgVectorDocumentStore


def test_retired_remote_embedding_provider_never_sends_data():
    with pytest.raises(ValueError, match="retired"):
        OpenAICompatibleEmbeddingProvider("https://old-provider.invalid/embeddings")


def test_semantic_store_rejects_non_bge_dimension() -> None:
    class Provider:
        dimension = 64

        def embed(self, text: str):
            return [0.0] * self.dimension

    with pytest.raises(ValueError, match="1024"):
        SemanticPgVectorDocumentStore(None, Provider())
