from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.versioning import ArtifactKind, VersionRegistry  # noqa: E402


def _build_registry() -> VersionRegistry:
    registry = VersionRegistry()
    artifacts = [
        (
            "mcp-tool-protocol",
            ArtifactKind.TOOL_PROTOCOL,
            "24",
            ROOT / "packages" / "mcp" / "server.py",
        ),
        (
            "browser-bridge-protocol",
            ArtifactKind.TOOL_PROTOCOL,
            "4",
            ROOT / "extension" / "protocol.json",
        ),
        (
            "semantic-rag-schema",
            ArtifactKind.KNOWLEDGE_SCHEMA,
            "1",
            ROOT / "migrations" / "003_semantic_rag.sql",
        ),
        (
            "personal-knowledge-schema",
            ArtifactKind.KNOWLEDGE_SCHEMA,
            "1",
            ROOT / "migrations" / "022_personal_knowledge.sql",
        ),
        (
            "candidate-profile-schema",
            ArtifactKind.KNOWLEDGE_SCHEMA,
            "1",
            ROOT / "packages" / "candidate_profile" / "models.py",
        ),
        (
            "website-blind-30",
            ArtifactKind.EVAL_FIXTURE,
            "1",
            ROOT / "evals" / "fixtures" / "website_blind_30.json",
        ),
        (
            "rag-frozen-cases",
            ArtifactKind.EVAL_FIXTURE,
            "1",
            ROOT / "evals" / "fixtures" / "rag_cases.json",
        ),
        (
            "unknown-site-strategy-cases",
            ArtifactKind.EVAL_FIXTURE,
            "1",
            ROOT / "evals" / "fixtures" / "unknown_site_strategy_cases.json",
        ),
        (
            "unknown-site-strategy-live-result",
            ArtifactKind.EVAL_FIXTURE,
            "1",
            ROOT / "evals" / "results" / "unknown_site_strategy_20260831.json",
        ),
    ]
    for name, kind, version, path in artifacts:
        registry.register(
            name=name,
            kind=kind,
            version=version,
            content=path.read_bytes(),
            source_ref=path.relative_to(ROOT).as_posix(),
        )
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify the artifact version manifest.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed manifest without modifying it",
    )
    args = parser.parse_args(argv)

    registry = _build_registry()
    destination = ROOT / "docs" / "ARTIFACT_VERSIONS.json"
    if args.check:
        with tempfile.TemporaryDirectory(prefix="recruitops-artifacts-") as temp_dir:
            candidate = Path(temp_dir) / destination.name
            registry.export(candidate)
            if not destination.exists() or destination.read_bytes() != candidate.read_bytes():
                print(
                    "artifact manifest is stale; run scripts/build_artifact_manifest.py",
                    file=sys.stderr,
                )
                return 1
        return 0

    registry.export(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
