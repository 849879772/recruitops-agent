from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete

from packages.rag import (
    DeterministicEmbeddingProvider,
    PersistentRagIndexer,
    PgVectorDocumentStore,
    SourceDocument,
)
from packages.rag.pgvector_store import document_chunks
from packages.storage import create_storage_engine


@pytest.mark.skipif(
    not os.getenv("RECRUITOPS_TEST_DATABASE_URL"),
    reason="set RECRUITOPS_TEST_DATABASE_URL to run the PostgreSQL integration test",
)
def test_real_postgres_sync_update_idempotency_and_managed_prune() -> None:
    engine = create_storage_engine(os.environ["RECRUITOPS_TEST_DATABASE_URL"])
    store = PgVectorDocumentStore(engine, DeterministicEmbeddingProvider())
    store.ensure_schema()
    source = f"integration_test_{uuid4().hex}"
    first_ref = "integration://first"
    stale_ref = "integration://stale"
    indexer = PersistentRagIndexer(store)
    try:
        first = indexer.sync(
            SourceDocument(
                source=source,
                source_ref=first_ref,
                content="old crawler recipe",
                metadata={"domain": "crawler", "revision": 1},
            )
        )
        changed = indexer.sync(
            SourceDocument(
                source=source,
                source_ref=first_ref,
                content="new crawler recipe with pagination",
                metadata={"domain": "crawler", "revision": 2},
            )
        )
        unchanged = indexer.sync(
            SourceDocument(
                source=source,
                source_ref=first_ref,
                content="new crawler recipe with pagination",
                metadata={"domain": "crawler", "revision": 2},
            )
        )
        indexer.sync(
            SourceDocument(
                source=source,
                source_ref=stale_ref,
                content="stale source",
                metadata={"domain": "crawler"},
            )
        )
        pruned = store.prune_managed_sources(
            managed_sources=[source],
            keep_source_refs={source: {first_ref}},
        )

        assert first.changed_chunks == 1
        assert changed.changed_chunks == 1
        assert changed.deleted_chunks == 1
        assert unchanged.unchanged_chunks == 1
        assert pruned == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(document_chunks).where(document_chunks.c.source == source)
            )
        engine.dispose()
