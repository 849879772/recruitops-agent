import pytest

from packages.versioning import ArtifactKind, VersionConflictError, VersionRegistry


def test_versions_are_content_bound_and_conflicts_fail_closed() -> None:
    registry = VersionRegistry()
    first = registry.register(
        name="typed-tools",
        kind=ArtifactKind.TOOL_PROTOCOL,
        version="1",
        content={"tools": ["search_jobs"]},
        source_ref="packages/mcp/server.py",
    )
    same = registry.register(
        name="typed-tools",
        kind=ArtifactKind.TOOL_PROTOCOL,
        version="1",
        content={"tools": ["search_jobs"]},
        source_ref="packages/mcp/server.py",
    )

    assert same.sha256 == first.sha256
    with pytest.raises(VersionConflictError):
        registry.register(
            name="typed-tools",
            kind=ArtifactKind.TOOL_PROTOCOL,
            version="1",
            content={"tools": ["search_jobs", "write_anything"]},
            source_ref="packages/mcp/server.py",
        )
