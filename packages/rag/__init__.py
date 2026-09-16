"""Local-first retrieval primitives for the RecruitOps knowledge base."""

from .embeddings import (
    DEFAULT_EMBEDDING_DIMENSION,
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from .models import Citation, DocumentChunk, RetrievalResult
from .indexing import (
    IncrementalRagIndex,
    SourceDocument,
    chunk_document,
    chunk_documents,
    split_document,
)
from .persistent_index import DocumentSyncStats, PersistentRagIndexer, RagSyncStats
from .retriever import (
    LexicalCosineRetriever,
    Retriever,
    cosine_similarity,
    lexical_score,
)
from .pgvector_store import PgVectorDocumentStore
from .semantic_pgvector_store import SemanticPgVectorDocumentStore
from .manifest import (
    ManifestSource,
    RagManifest,
    documents_from_manifest,
    load_manifest,
    load_manifest_documents,
    preview_documents,
    validate_unique_documents,
)
from .grounding import EvidenceGrounder, GroundedEvidence
from .ingest import (
    clean_source_text,
    document_from_profile_config,
    documents_from_json_records,
    documents_from_structured_json_file,
    documents_from_text_files,
    load_json_records,
)
from .lujie_resume import documents_from_lujie_sqlite

__all__ = [
    "Citation",
    "DEFAULT_EMBEDDING_DIMENSION",
    "DeterministicEmbeddingProvider",
    "DocumentChunk",
    "DocumentSyncStats",
    "EmbeddingProvider",
    "OpenAICompatibleEmbeddingProvider",
    "EvidenceGrounder",
    "GroundedEvidence",
    "clean_source_text",
    "document_from_profile_config",
    "documents_from_json_records",
    "documents_from_structured_json_file",
    "documents_from_lujie_sqlite",
    "documents_from_text_files",
    "load_json_records",
    "LexicalCosineRetriever",
    "PgVectorDocumentStore",
    "SemanticPgVectorDocumentStore",
    "ManifestSource",
    "RagManifest",
    "documents_from_manifest",
    "load_manifest",
    "load_manifest_documents",
    "preview_documents",
    "validate_unique_documents",
    "IncrementalRagIndex",
    "PersistentRagIndexer",
    "RagSyncStats",
    "Retriever",
    "RetrievalResult",
    "SourceDocument",
    "chunk_document",
    "chunk_documents",
    "cosine_similarity",
    "lexical_score",
    "split_document",
]
