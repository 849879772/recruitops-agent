import json

import pytest
from fastapi.testclient import TestClient

from packages.rag.embeddings import OpenAICompatibleEmbeddingProvider, QWEN_QUERY_PREFIX
from services.embedding.server import create_app


def test_personal_embedding_override_preserves_legacy_configuration(monkeypatch):
    from packages.config import Settings
    from packages import personal_knowledge as knowledge
    settings = Settings(_env_file=None, embedding_endpoint="http://legacy.test/embeddings",
                        embedding_model="legacy", embedding_api_key="legacy-key",
                        knowledge_embedding_endpoint="http://qwen.test/v1/embeddings",
                        knowledge_embedding_api_key="local-key")
    monkeypatch.setattr(knowledge, "get_settings", lambda: settings)
    monkeypatch.setattr(knowledge.Storage, "from_url", lambda *_: object())
    knowledge.get_knowledge_service.cache_clear()
    try:
        provider = knowledge.get_knowledge_service().provider
        assert provider.endpoint == settings.knowledge_embedding_endpoint
        assert provider.api_key == "local-key"
        assert provider.query_prefix == QWEN_QUERY_PREFIX
        assert settings.embedding_endpoint == "http://legacy.test/embeddings"
        settings.knowledge_embedding_endpoint = ""
        knowledge.get_knowledge_service.cache_clear()
        fallback = knowledge.get_knowledge_service().provider
        assert fallback.model == "legacy"
        assert fallback.api_key == "legacy-key"
        assert fallback.query_prefix == ""
    finally:
        knowledge.get_knowledge_service.cache_clear()


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


def test_batched_client_orders_results_and_instructs_only_queries(monkeypatch):
    requests = []

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.value).encode()

    def request(req, **kwargs):
        value = json.loads(req.data)
        requests.append(value)
        return Response({"data": [{"index": i, "embedding": [float(i + 1), 1]} for i in reversed(range(len(value["input"])))]})

    monkeypatch.setattr("packages.rag.embeddings.urlopen", request)
    provider = OpenAICompatibleEmbeddingProvider("http://local.test/v1/embeddings", dimension=2, query_prefix=QWEN_QUERY_PREFIX)
    assert len(provider.embed_many(["document"] * 18)) == 18
    assert [len(r["input"]) for r in requests] == [16, 2]
    assert provider.embed_many(["a", "b"]) == [[1., 1.], [2., 1.]]
    provider.embed_query("question")
    assert requests[-1]["input"] == [QWEN_QUERY_PREFIX + "question"]
    assert requests[0]["input"][0] == "document"
    assert provider.version != OpenAICompatibleEmbeddingProvider("http://local.test", dimension=2).version
    for rows in ([], [{"index": 4, "embedding": [1, 1]}], [{"index": 0, "embedding": [0, 0]}]):
        monkeypatch.setattr("packages.rag.embeddings.urlopen", lambda *a, **k: Response({"data": rows}))
        with pytest.raises((RuntimeError, ValueError)):
            provider.embed("bad")
