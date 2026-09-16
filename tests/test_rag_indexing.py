from packages.rag import (
    IncrementalRagIndex,
    SourceDocument,
    chunk_document,
    split_document,
)


def test_split_document_is_bounded_and_keeps_content() -> None:
    text = "职位描述\n\n" + ("C++ 与 ROS 开发。" * 120)
    chunks = split_document(text, max_chars=160, overlap=20)

    assert chunks
    assert all(len(chunk) <= 160 for chunk in chunks)
    assert "职位描述" in chunks[0]
    assert "C++" in "".join(chunks)


def test_incremental_index_skips_unchanged_source_and_replaces_changed_source() -> None:
    index = IncrementalRagIndex()
    original = SourceDocument(
        source="resume",
        source_ref="resume://project/1",
        content="Python 服务开发",
        metadata={"kind": "project"},
    )

    assert index.upsert(original) is True
    assert index.upsert(original) is False
    assert index.retriever.search("Python", top_k=1)[0].chunk.content == "Python 服务开发"

    changed = SourceDocument(
        source=original.source,
        source_ref=original.source_ref,
        content="C++ ROS 机械臂开发",
        metadata=original.metadata,
    )
    assert index.upsert(changed) is True
    assert index.retriever.search("C++", top_k=1)[0].chunk.content == "C++ ROS 机械臂开发"


def test_chunk_document_has_stable_ids_and_fingerprint() -> None:
    document = SourceDocument(
        source="crawler_knowledge",
        source_ref="docs://moka",
        content="分页规则\n\n详情接口",
        metadata={"domain": "crawler"},
    )
    first = chunk_document(document, max_chars=80, overlap=10)
    second = chunk_document(document, max_chars=80, overlap=10)

    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    assert first[0].metadata["document_fingerprint"]
    assert first[0].metadata["domain"] == "crawler"
