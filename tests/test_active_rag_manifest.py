from pathlib import Path

from packages.rag.manifest import load_manifest


ROOT = Path(__file__).resolve().parents[1]


def test_active_rag_manifest_contains_only_crawler_knowledge() -> None:
    manifest = load_manifest(ROOT / "config" / "rag_sources.yaml")

    assert set(manifest.managed_sources) == {
        "crawler_knowledge",
        "crawler_recipe",
        "campaign_evidence",
    }
    assert all(source.kind not in {"profile_config", "lujie_resume"} for source in manifest.sources)
    assert all(source.metadata.get("domain") == "crawler" for source in manifest.sources)
