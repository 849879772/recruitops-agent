import json

import pytest

from packages.rag import OpenAICompatibleEmbeddingProvider, SemanticPgVectorDocumentStore


def test_openai_compatible_provider_validates_embedding_dimension(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"data": [{"embedding": [0.1] * 1024}]}).encode()

    monkeypatch.setattr("packages.rag.embeddings.urlopen", lambda *args, **kwargs: Response())
    provider = OpenAICompatibleEmbeddingProvider("http://embed.test/v1/embeddings")
    assert len(provider.embed("ROS 机械臂")) == 1024


def test_semantic_store_rejects_non_bge_dimension() -> None:
    class Provider:
        dimension = 64

        def embed(self, text: str):
            return [0.0] * self.dimension

    with pytest.raises(ValueError, match="1024"):
        SemanticPgVectorDocumentStore(None, Provider())
