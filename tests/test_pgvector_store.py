import inspect

from packages.rag.pgvector_store import PgVectorDocumentStore, document_chunks
from packages.rag.semantic_pgvector_store import SemanticPgVectorDocumentStore


def test_pgvector_store_contract_matches_migration() -> None:
    assert isinstance(document_chunks.c.embedding.type, object)
    assert document_chunks.c.embedding.type.dim == 64
    assert "source_ref" in document_chunks.c
    assert "chunk_index" in document_chunks.c
    assert PgVectorDocumentStore.__name__ == "PgVectorDocumentStore"


def test_pgvector_searches_probe_all_existing_ivfflat_lists() -> None:
    assert "SET LOCAL ivfflat.probes = 100" in inspect.getsource(PgVectorDocumentStore.search)
    assert "SET LOCAL ivfflat.probes = 100" in inspect.getsource(
        SemanticPgVectorDocumentStore.search
    )
