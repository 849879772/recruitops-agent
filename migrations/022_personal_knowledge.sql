CREATE TABLE IF NOT EXISTS personal_knowledge_documents (
    id VARCHAR(32) PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    revision VARCHAR(32) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    status VARCHAR(20) NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    pages JSON NOT NULL DEFAULT '[]',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_knowledge_chunks (
    id VARCHAR(32) PRIMARY KEY,
    document_id VARCHAR(32) NOT NULL REFERENCES personal_knowledge_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    page INTEGER NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR NOT NULL,
    model VARCHAR(255) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_personal_knowledge_chunks_document_id ON personal_knowledge_chunks(document_id);
