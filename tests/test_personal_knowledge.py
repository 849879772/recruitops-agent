import base64
import io
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import func, select

from packages.personal_knowledge import KnowledgeService, KnowledgeDocument, KnowledgeChunk, extract_pages, split_pages, tokenize
from packages.storage import Storage
from packages.tools.knowledge import KnowledgeSearchInput, search_personal_knowledge


class TestEmbedding:
    __test__ = False
    dimension = 3
    version = "test-semantic-v1"

    def __init__(self):
        self.calls = []

    def embed(self, text):
        self.calls.append(text)
        if any(word in text for word in ("视觉", "相机", "看到", "定位")):
            return [1., 0., 0.]
        if "ROS2" in text:
            return [0., 1., 0.]
        return [0., 0., 1.]


@pytest.fixture
def service(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'kb.db'}", initialize=True)
    yield KnowledgeService(storage, TestEmbedding())
    storage.engine.dispose()


def ingest(service, text, name="notes.md", kind="notes"):
    doc = service.queue(name, text.encode(), kind)
    service.ingest(doc["id"], doc["revision"])
    return doc


def test_import_search_read_reuse_delete(service):
    doc = ingest(service, "# 视觉\n\n使用相机和手眼标定完成目标定位。", kind="reference")
    assert service.list_documents()[0]["status"] == "ready"
    calls = len(service.provider.calls)
    again = service.queue("copy.md", "# 视觉\n\n使用相机和手眼标定完成目标定位。".encode(), "reference")
    assert again["id"] == doc["id"] and again["reused"]
    assert len(service.provider.calls) == calls
    hits = service.search("手眼标定")
    assert hits[0]["document_id"] == doc["id"]
    assert hits[0]["kind"] == "reference"
    assert hits[0]["rrf"] > 0 and "revision=" in hits[0]["url"]
    assert "手眼标定" in service.read(doc["id"])["text"]
    service.delete_document(doc["id"], doc["revision"])
    assert service.search("手眼标定") == []
    with service.storage.session() as session:
        assert session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
    with pytest.raises(LookupError):
        service.read(doc["id"])


def test_semantic_and_keyword_routes_rrf(service):
    a = ingest(service, "使用相机识别目标", "a.md")
    b = ingest(service, "ROS2 使用节点通信", "b.md")
    hits = service.search("机器人如何看到物体")
    assert hits[0]["document_id"] == a["id"] and hits[0]["bm25"] == 0
    hits = service.search("ROS2")
    assert hits[0]["document_id"] == b["id"]
    assert hits[0]["rrf"] == pytest.approx(2 / 61)
    assert "ros2" in tokenize("ROS2 手眼标定")


def test_version_scope_and_short_full_read(service):
    old = ingest(service, "# 旧材料\n\n相机标定", "a.md")
    ingest(service, "其他领域材料", "b.md")
    assert service.search("无关词", document_id=old["id"])[0]["mode"] == "full_document"
    new = service.queue("a.md", "ROS2 新材料".encode(), "personal", old["id"], old["revision"])
    assert service.search("标定", document_id=old["id"]) == []
    service.ingest(new["id"], new["revision"])
    with pytest.raises(ValueError, match="旧版本"):
        service.read(old["id"], revision=old["revision"])
    assert service.read(new["id"])["kind"] == "personal"
    with pytest.raises(ValueError, match="刷新"):
        service.delete_document(old["id"], old["revision"])


def test_failure_retry_and_interrupt(service):
    doc = service.queue("test.md", "视觉定位".encode())
    original = service.provider.embed
    service.provider.embed = Mock(side_effect=RuntimeError("secret remote URL"))
    service.ingest(doc["id"], doc["revision"])
    failed = service.list_documents()[0]
    assert failed["status"] == "failed" and "secret" not in failed["error"]
    service.provider.embed = original
    retried = service.retry(doc["id"], doc["revision"])
    service.ingest(retried["id"], retried["revision"])
    assert service.list_documents()[0]["status"] == "ready"
    pending = service.queue("pending.md", b"pending")
    service.recover_interrupted()
    assert next(d for d in service.list_documents() if d["id"] == pending["id"])["status"] == "failed"


def test_deleted_while_embedding_never_reappears(service):
    doc = service.queue("race.md", b"test")
    def embed(_):
        service.delete_document(doc["id"], doc["revision"])
        return [1., 0., 0.]
    service.provider.embed = embed
    service.ingest(doc["id"], doc["revision"])
    assert service.list_documents() == []


def test_invalid_files_and_chunk_boundaries(service):
    for name, content in [("../bad.md", b"x"), ("weights.pt", b"x"), ("empty.txt", b""), ("big.txt", b"x" * 5_000_001)]:
        with pytest.raises(ValueError):
            service.queue(name, content)
    doc = ingest(service, "\x00binary")
    assert service.list_documents()[0]["status"] == "failed"
    writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
    output = io.BytesIO(); writer.write(output)
    with pytest.raises(ValueError, match="无法提取"):
        extract_pages("scan.pdf", output.getvalue())
    chunks = split_pages(["# 标定\n\n" + "目标位置。" * 200, "第二页内容"])
    assert all(len(c["content"]) <= 350 for c in chunks)
    assert chunks[-1]["page"] == 2
    assert chunks[1]["section"] == "标定"


def test_model_change_never_mixes_vectors(service):
    ingest(service, "视觉定位")
    service.provider.version = "other-model"
    assert service.search("视觉") == []


def test_reindex_preserves_document_and_old_vectors_until_success(service):
    doc = ingest(service, "视觉定位")
    assert service.reindex(doc["id"]) is False
    service.provider.version = "new-model"
    original = service.provider.embed
    service.provider.embed = Mock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError):
        service.reindex(doc["id"])
    with service.storage.session() as session:
        assert session.scalar(select(KnowledgeChunk.model)) == "test-semantic-v1"
    service.provider.embed = original
    assert service.reindex(doc["id"]) is True
    assert service.read(doc["id"], revision=doc["revision"])["text"] == "视觉定位"
    assert service.search("视觉")[0]["document_id"] == doc["id"]
    assert service.reindex(doc["id"]) is False


def test_mcp_read_only_list_scope_and_offsets(service):
    doc = ingest(service, "视觉资料。" * 1400)
    listing = search_personal_knowledge(KnowledgeSearchInput(domain="personal", action="list"), service)
    assert listing.success and listing.read_only and listing.data.documents
    read = search_personal_knowledge(KnowledgeSearchInput(domain="personal", action="read", document_id=doc["id"]), service)
    assert read.success and read.data.next_offset == 6000
    assert read.data.citations[0].metadata["revision"] == doc["revision"]
    result = search_personal_knowledge(KnowledgeSearchInput(query="视觉", domain="personal"), service)
    assert result.success and result.evidence and result.data.citations
    missing = search_personal_knowledge(KnowledgeSearchInput(domain="personal", action="read", document_id="a" * 32), service)
    assert not missing.success and missing.error_message
    empty = search_personal_knowledge(KnowledgeSearchInput(domain="personal", query="test", document_id="b" * 32), service)
    assert not empty.success and empty.status.value == "no_results"
    with pytest.raises(ValueError):
        KnowledgeSearchInput(query="", domain="personal")
    with pytest.raises(ValueError):
        KnowledgeSearchInput(action="read", domain="personal", document_id="../file")


def test_owner_api_upload_preview_delete_and_boundary(service, monkeypatch):
    from apps.api import knowledge, main
    monkeypatch.setattr(knowledge, "get_knowledge_service", lambda: service)
    client = TestClient(main.app, base_url="http://127.0.0.1:8012")
    headers = {"Origin": "http://127.0.0.1:8012", "X-RecruitOps-Local-UI": "1"}
    url = "/api/local-ui/knowledge"
    assert client.post(url + "/list").status_code == 403
    assert client.post(url + "/list", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    data = {"filename": "<b>safe.md", "kind": "notes", "content_base64": base64.b64encode("# 标定\n\n个人视觉资料".encode()).decode()}
    result = client.post(url + "/upload", headers=headers, json=data)
    assert result.status_code == 200, result.text
    doc = result.json()
    listing = client.post(url + "/list", headers=headers).json()
    assert listing["documents"][0]["status"] == "ready"
    request = {"document_id": doc["id"], "revision": doc["revision"]}
    assert client.post(url + "/read", headers=headers, json=request).json()["text"].startswith("# 标定")
    assert client.post(url + "/delete", headers=headers, json=request).status_code == 200


def test_job_and_knowledge_context_reach_turn_and_existing_prompt_unchanged():
    from apps.api.main import CodexTurnStartRequest
    assert CodexTurnStartRequest(text="处理邮件").prompt() == "处理邮件"
    prompt = CodexTurnStartRequest(text="结合我的笔记分析", job_id="job-123", knowledge_enabled=True, knowledge_document_id="a" * 32).prompt()
    assert '"job_id": "job-123"' in prompt and '"document_id": "' + "a" * 32 in prompt
    assert "knowledge_search" in prompt
