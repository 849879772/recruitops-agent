import json

import pytest
from fastapi.testclient import TestClient

from packages.rag.embeddings import OpenAICompatibleEmbeddingProvider, QWEN_QUERY_PREFIX
from services.embedding.server import create_app


class Engine:
    def __init__(self):
        self.calls = []

    def load(self):
        pass

    def encode(self, texts):
        self.calls.append(texts)
        return [[1.] + [0.] * 1023 for _ in texts], len(texts) * 3


def test_service_authentication_limits_and_protocol():
    engine = Engine()
    client = TestClient(create_app(engine, "a" * 48))
    headers = {"Authorization": "Bearer " + "a" * 48}
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=headers).json()["device"] == "cuda"
    assert client.post("/v1/embeddings", json={"input": ["private"]}).status_code == 401
    assert not engine.calls
    result = client.post("/v1/embeddings", headers=headers, json={"input": ["first", "second"]})
    assert result.status_code == 200
    assert [row["index"] for row in result.json()["data"]] == [0, 1]
    assert len(result.json()["data"][0]["embedding"]) == 1024
    for data in ({"input": []}, {"input": [""]}, {"input": ["x"] * 17}, {"input": ["x" * 12001]}, {"input": ["x"], "model": "other"}):
        assert client.post("/v1/embeddings", headers=headers, json=data).status_code == 422
    assert len(engine.calls) == 1
    assert client.post("/v1/embeddings", headers=headers, content=b"x" * 800001).status_code == 413


def test_retired_remote_embedding_provider_never_sends_data():
    with pytest.raises(ValueError, match="retired"):
        OpenAICompatibleEmbeddingProvider("https://old-provider.invalid/embeddings")
