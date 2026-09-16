"""Import approved crawler knowledge and candidate evidence into Agent-owned RAG storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.config import Settings
from packages.rag import (
    DeterministicEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    PersistentRagIndexer,
    PgVectorDocumentStore,
    SemanticPgVectorDocumentStore,
    document_from_profile_config,
    documents_from_text_files,
    load_manifest_documents,
    preview_documents,
    validate_unique_documents,
)
from packages.storage import create_storage_engine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import explicitly approved local text sources into Agent-owned RAG storage."
    )
    parser.add_argument(
        "--crawler-root",
        action="append",
        default=[],
        type=Path,
        help="approved ATS/crawler knowledge directory; may be repeated",
    )
    parser.add_argument(
        "--profile-config",
        type=Path,
        help="explicit config.yaml path; only the allowlisted profile section is read",
    )
    parser.add_argument(
        "--candidate-root",
        action="append",
        default=[],
        type=Path,
        help="approved resume/project/evidence directory; may be repeated",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="version-1 YAML source manifest; paths are relative to the manifest",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="connect to the configured store and apply the synchronization",
    )
    parser.add_argument(
        "--prune-managed",
        action="store_true",
        help="with --apply, remove stale documents only in manifest managed_sources",
    )
    parser.add_argument(
        "--backend",
        choices=("offline", "semantic"),
        default="offline",
        help="offline uses deterministic test vectors; semantic requires an endpoint",
    )
    parser.add_argument("--database-url", help="override RECRUITOPS_DATABASE_URL")
    return parser


def _validated_roots(values: list[Path], label: str) -> list[Path]:
    roots: list[Path] = []
    for value in values:
        root = value.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"{label} root is not a readable directory: {root}")
        roots.append(root)
    return roots


def _load_documents(
    crawler_roots: list[Path],
    candidate_roots: list[Path],
    profile_config: Path | None = None,
):
    documents = []
    for root in crawler_roots:
        documents.extend(
            documents_from_text_files(
                root,
                source="crawler_knowledge",
                metadata={"domain": "crawler", "trust": "approved"},
            )
        )
    for root in candidate_roots:
        documents.extend(
            documents_from_text_files(
                root,
                source="candidate_evidence",
                metadata={"domain": "candidate", "trust": "approved"},
            )
        )
    if profile_config is not None:
        profile_path = profile_config.expanduser().resolve()
        if not profile_path.is_file():
            raise ValueError(f"profile config is not a readable file: {profile_path}")
        documents.append(document_from_profile_config(profile_path))
    return validate_unique_documents(documents)


def _load_requested_documents(args: argparse.Namespace):
    legacy_inputs = bool(args.crawler_root or args.candidate_root or args.profile_config)
    if args.manifest and legacy_inputs:
        raise SystemExit("--manifest cannot be combined with legacy source arguments")
    if args.manifest:
        manifest, documents = load_manifest_documents(args.manifest)
        return manifest, documents

    crawler_roots = _validated_roots(args.crawler_root, "crawler")
    candidate_roots = _validated_roots(args.candidate_root, "candidate")
    if not crawler_roots and not candidate_roots and args.profile_config is None:
        raise SystemExit(
            "at least one --manifest, --crawler-root, --candidate-root, or "
            "--profile-config is required"
        )
    return None, _load_documents(crawler_roots, candidate_roots, args.profile_config)


def _keep_source_refs(documents) -> dict[str, set[str]]:
    return {
        source: {document.source_ref for document in documents if document.source == source}
        for source in {document.source for document in documents}
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest, documents = _load_requested_documents(args)
    if args.prune_managed and manifest is None:
        raise SystemExit("--prune-managed requires --manifest")
    if args.prune_managed:
        if not manifest.managed_sources:
            raise SystemExit("--prune-managed requires a non-empty managed_sources list")
        if not documents:
            raise SystemExit("--prune-managed requires at least one document")

    preview = preview_documents(documents)
    preview["apply"] = args.apply
    preview["prune_managed"] = args.prune_managed
    if manifest is not None:
        preview["managed_sources"] = list(manifest.managed_sources)
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, sort_keys=True))
        return 0

    settings = Settings()
    database_url = args.database_url or settings.database_url
    if args.backend == "semantic":
        if not settings.embedding_endpoint:
            raise SystemExit("semantic backend requires RECRUITOPS_EMBEDDING_ENDPOINT")
        provider = OpenAICompatibleEmbeddingProvider(
            settings.embedding_endpoint,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key or None,
            dimension=settings.embedding_dimension,
        )
    else:
        provider = DeterministicEmbeddingProvider()

    engine = create_storage_engine(database_url)
    try:
        if args.backend == "semantic":
            store = SemanticPgVectorDocumentStore(engine, provider)
        else:
            store = PgVectorDocumentStore(engine, provider)
        store.ensure_schema()
        result = PersistentRagIndexer(store).sync_many(documents)
        pruned_chunks = 0
        if args.prune_managed:
            pruned_chunks = store.prune_managed_sources(
                managed_sources=manifest.managed_sources,
                keep_source_refs=_keep_source_refs(documents),
            )
    finally:
        engine.dispose()

    output = dict(result.__dict__)
    output["pruned_chunks"] = pruned_chunks
    output["deleted_chunks"] += pruned_chunks
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
