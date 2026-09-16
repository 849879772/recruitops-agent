from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from packages.rag import (
    DeterministicEmbeddingProvider,
    DocumentChunk,
    PgVectorDocumentStore,
    SemanticPgVectorDocumentStore,
    documents_from_manifest,
    load_manifest,
    preview_documents,
)


def test_manifest_resolves_all_supported_sources_and_keeps_profile_allowlist(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "single.md").write_text("single source", encoding="utf-8")
    (tmp_path / "nested" / "root.txt").write_text("root source", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "profile:\n  skills: [Python]\n  direction: 后端\n"
        "deepseek:\n  api_key: do-not-import\ncompanies: []\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        "version: 1\n"
        "managed_sources: [docs]\n"
        "sources:\n"
        "  - kind: text_file\n"
        "    path: single.md\n"
        "    source: docs\n"
        "    metadata: {domain: single}\n"
        "  - kind: text_root\n"
        "    path: nested\n"
        "    source: docs\n"
        "    metadata: {trust: approved}\n"
        "  - kind: profile_config\n"
        "    path: config.yaml\n"
        "    source: profile\n"
        "    metadata: {owner: candidate}\n",
        encoding="utf-8",
    )

    documents = documents_from_manifest(manifest_path)

    assert [(document.source, document.source_ref) for document in documents] == [
        ("docs", (tmp_path / "single.md").resolve().as_posix()),
        ("docs", (tmp_path / "nested" / "root.txt").resolve().as_posix()),
        ("profile", (tmp_path / "config.yaml").resolve().as_posix()),
    ]
    assert documents[0].metadata["domain"] == "single"
    assert documents[1].metadata["trust"] == "approved"
    assert documents[2].metadata["owner"] == "candidate"
    assert "Python" in documents[2].content
    assert "do-not-import" not in documents[2].content
    assert "companies" not in documents[2].content


def test_manifest_loads_json_and_lujie_resume_sources(tmp_path: Path) -> None:
    import sqlite3

    json_path = tmp_path / "recipes.json"
    json_path.write_text('{"Moka": {"pagination": "page"}}', encoding="utf-8")
    database = tmp_path / "lujie.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE ResumeVersion (id TEXT, jobId TEXT, content TEXT);"
        "CREATE TABLE Resume (id TEXT, content TEXT);"
    )
    connection.execute(
        "INSERT INTO ResumeVersion VALUES (?, ?, ?)",
        ("original", None, '{"skills": ["C++", "ROS"]}'),
    )
    connection.commit()
    connection.close()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "sources:\n"
        "  - kind: json_file\n"
        "    path: recipes.json\n"
        "    source: crawler_recipe\n"
        "    metadata: {domain: crawler}\n"
        "  - kind: lujie_resume\n"
        "    path: lujie.db\n"
        "    record_ids: [original]\n"
        "    metadata: {import: local}\n",
        encoding="utf-8",
    )

    documents = documents_from_manifest(manifest)

    assert [document.source for document in documents] == [
        "crawler_recipe",
        "candidate_evidence",
    ]
    assert documents[0].metadata["domain"] == "crawler"
    assert documents[1].metadata["domain"] == "candidate"
    assert documents[1].metadata["import"] == "local"
    assert "C++" in documents[1].content


def test_manifest_requires_explicit_lujie_record_selection(tmp_path: Path) -> None:
    database = tmp_path / "lujie.db"
    database.write_bytes(b"not-opened-during-manifest-validation")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "version: 1\nsources:\n  - kind: lujie_resume\n    path: lujie.db\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit record_ids"):
        load_manifest(manifest)


def test_manifest_rejects_unknown_kind_missing_file_and_duplicate_source_ref(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.md").write_text("one", encoding="utf-8")

    unknown_kind = tmp_path / "unknown.yaml"
    unknown_kind.write_text(
        "version: 1\nsources:\n  - kind: database\n    path: one.md\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="kind"):
        load_manifest(unknown_kind)

    missing_file = tmp_path / "missing.yaml"
    missing_file.write_text(
        "version: 1\nsources:\n  - kind: text_file\n    path: missing.md\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="readable file"):
        documents_from_manifest(missing_file)

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "version: 1\n"
        "sources:\n"
        "  - kind: text_file\n"
        "    path: one.md\n"
        "    source: docs\n"
        "  - kind: text_file\n"
        "    path: one.md\n"
        "    source: docs\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"duplicate source\+source_ref"):
        documents_from_manifest(duplicate)


def test_preview_contains_counts_and_fingerprints_but_not_document_text(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("private body", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "version: 1\nsources:\n  - kind: text_file\n    path: source.md\n",
        encoding="utf-8",
    )

    documents = documents_from_manifest(manifest)
    preview = preview_documents(documents)
    serialized = json.dumps(preview, ensure_ascii=False)

    assert preview["documents"] == 1
    assert preview["chunks"] == 1
    assert preview["characters"] == len("private body")
    assert preview["source_summaries"][0]["content_fingerprint"] == hashlib.sha256(
        b"private body"
    ).hexdigest()
    assert "private body" not in serialized


class _Result:
    def __init__(self, rows=(), rowcount: int = 1):
        self.rows = list(rows)
        self.rowcount = rowcount

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if getattr(statement, "is_select", False):
            return _Result(self.rows, rowcount=0)
        return _Result(rowcount=1)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _Engine:
    def __init__(self, rows=()):
        self.connection = _Connection(rows)

    def begin(self):
        return _Transaction(self.connection)


@pytest.mark.parametrize(
    ("store_class", "dimension"),
    [
        (PgVectorDocumentStore, 64),
        (SemanticPgVectorDocumentStore, 1024),
    ],
)
def test_sync_deletes_replaced_chunk_before_upsert_and_counts_it(
    store_class,
    dimension: int,
) -> None:
    provider = DeterministicEmbeddingProvider(dimension=dimension)
    engine = _Engine(
        [
            {
                "id": "old-id",
                "content_hash": hashlib.sha256(b"old body").hexdigest(),
                "embedding_model": provider.version,
                "chunk_index": 0,
                "metadata": {"chunk_index": 0},
            }
        ]
    )
    store = store_class(engine, provider)
    chunk = DocumentChunk(
        id="new-id",
        content="new body",
        source="docs",
        source_ref="doc-1",
        metadata={"chunk_index": 0},
    )

    result = store.sync_chunks(source="docs", source_ref="doc-1", chunks=[chunk])

    statements = engine.connection.statements
    assert statements[0].is_select
    assert statements[1].is_delete
    assert statements[2].is_insert
    assert result.changed_chunks == 1
    assert result.deleted_chunks == 1


@pytest.mark.parametrize(
    ("store_class", "dimension"),
    [
        (PgVectorDocumentStore, 64),
        (SemanticPgVectorDocumentStore, 1024),
    ],
)
def test_sync_updates_metadata_even_when_content_and_model_match(
    store_class,
    dimension: int,
) -> None:
    provider = DeterministicEmbeddingProvider(dimension=dimension)
    engine = _Engine(
        [
            {
                "id": "same-id",
                "content_hash": hashlib.sha256(b"same body").hexdigest(),
                "embedding_model": provider.version,
                "chunk_index": 0,
                "metadata": {"chunk_index": 0, "revision": "old"},
            }
        ]
    )
    store = store_class(engine, provider)
    chunk = DocumentChunk(
        id="same-id",
        content="same body",
        source="docs",
        source_ref="doc-1",
        metadata={"chunk_index": 0, "revision": "new"},
    )

    result = store.sync_chunks(source="docs", source_ref="doc-1", chunks=[chunk])

    assert engine.connection.statements[1].is_insert
    assert result.changed_chunks == 1
    assert result.unchanged_chunks == 0


@pytest.mark.parametrize(
    ("store_class", "dimension"),
    [
        (PgVectorDocumentStore, 64),
        (SemanticPgVectorDocumentStore, 1024),
    ],
)
def test_managed_prune_rejects_empty_scope_and_keeps_refs(
    store_class,
    dimension: int,
) -> None:
    engine = _Engine()
    store = store_class(engine, DeterministicEmbeddingProvider(dimension=dimension))

    with pytest.raises(ValueError, match="managed_sources"):
        store.prune_managed_sources(managed_sources=[], keep_source_refs={})
    with pytest.raises(ValueError, match="without documents"):
        store.prune_managed_sources(
            managed_sources=["docs"],
            keep_source_refs={"docs": set()},
        )

    assert (
        store.prune_managed_sources(
            managed_sources=["docs"],
            keep_source_refs={"docs": {"keep-me"}},
        )
        == 1
    )
    delete_statement = engine.connection.statements[-1]
    assert delete_statement.is_delete
    sql = str(delete_statement)
    assert "source_ref" in sql
