-- Optional semantic index for a real BGE-M3-compatible embedding service.
-- The deterministic 64-dimensional table remains available for offline tests.

CREATE TABLE IF NOT EXISTS semantic_document_chunks (
    id VARCHAR(255) PRIMARY KEY,
    content TEXT NOT NULL,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(2048) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0 CHECK (chunk_index >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash CHAR(64) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_semantic_chunks_source_ref_index
        UNIQUE (source, source_ref, chunk_index)
);

CREATE INDEX IF NOT EXISTS ix_semantic_document_chunks_metadata
    ON semantic_document_chunks USING GIN (metadata);

CREATE INDEX IF NOT EXISTS ix_semantic_document_chunks_embedding_cosine
    ON semantic_document_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
