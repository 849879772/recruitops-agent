-- Agent-owned RAG chunks. The autumn source SQLite/JSON store remains read-only.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_chunks (
    id VARCHAR(255) PRIMARY KEY,
    content TEXT NOT NULL,
    source VARCHAR(128) NOT NULL,
    source_ref VARCHAR(2048) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0 CHECK (chunk_index >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash CHAR(64) NOT NULL,
    embedding_model VARCHAR(128) NOT NULL DEFAULT 'deterministic-local-v1',
    embedding VECTOR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_document_chunks_source_ref_index
        UNIQUE (source, source_ref, chunk_index)
);

CREATE INDEX IF NOT EXISTS ix_document_chunks_metadata
    ON document_chunks USING GIN (metadata);

CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_cosine
    ON document_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
