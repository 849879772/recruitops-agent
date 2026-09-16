from packages.rag import DocumentSyncStats, PersistentRagIndexer, SourceDocument


class RecordingStore:
    def __init__(self):
        self.calls = []

    def sync_chunks(self, *, source, source_ref, chunks):
        self.calls.append((source, source_ref, chunks))
        return DocumentSyncStats(
            source=source,
            source_ref=source_ref,
            changed_chunks=len(chunks),
        )


def test_persistent_index_aggregates_document_stats() -> None:
    store = RecordingStore()
    documents = [
        SourceDocument(source="knowledge", source_ref="a", content="A", metadata={}),
        SourceDocument(source="knowledge", source_ref="b", content="B", metadata={}),
    ]

    result = PersistentRagIndexer(store).sync_many(documents)

    assert result.documents == 2
    assert result.changed_chunks == 2
    assert len(store.calls) == 2
