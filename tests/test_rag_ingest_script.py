from pathlib import Path
import json

import pytest

import scripts.ingest_rag_sources as ingest_script
from scripts.ingest_rag_sources import _load_documents, _validated_roots, build_parser


def test_ingest_script_requires_explicit_approved_roots(tmp_path: Path) -> None:
    root = tmp_path / "crawler"
    root.mkdir()
    (root / "moka.md").write_text("Moka 分页与详情接口", encoding="utf-8")

    documents = _load_documents(_validated_roots([root], "crawler"), [])

    assert len(documents) == 1
    assert documents[0].metadata == {
        "domain": "crawler",
        "trust": "approved",
        "path": (root / "moka.md").resolve().as_posix(),
    }


def test_ingest_script_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a readable directory"):
        _validated_roots([tmp_path / "missing"], "candidate")


def test_ingest_parser_defaults_to_offline() -> None:
    args = build_parser().parse_args(["--crawler-root", "."])
    assert args.backend == "offline"


def test_manifest_dry_run_does_not_create_database_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "notes.md").write_text("api_key=do-not-print", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "sources:\n"
        "  - kind: text_file\n"
        "    path: notes.md\n"
        "    source: notes\n"
        "    metadata: {secret: do-not-print}\n",
        encoding="utf-8",
    )

    def fail_if_engine_is_created(*args, **kwargs):
        raise AssertionError("dry-run must not create a database engine")

    monkeypatch.setattr(ingest_script, "create_storage_engine", fail_if_engine_is_created)

    assert ingest_script.main(["--manifest", str(manifest)]) == 0
    output = capsys.readouterr().out
    preview = json.loads(output)

    assert preview["mode"] == "dry-run"
    assert preview["documents"] == 1
    assert preview["chunks"] == 1
    assert preview["source_summaries"][0]["source"] == "notes"
    assert "do-not-print" not in output


def test_legacy_sources_are_dry_run_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "legacy.md").write_text("legacy source", encoding="utf-8")
    monkeypatch.setattr(
        ingest_script,
        "create_storage_engine",
        lambda *args, **kwargs: pytest.fail("legacy dry-run must not create an engine"),
    )

    assert ingest_script.main(["--crawler-root", str(tmp_path)]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "dry-run"
    assert preview["documents"] == 1


def test_prune_apply_requires_managed_sources_and_documents_before_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "empty.yaml"
    manifest.write_text(
        "version: 1\nmanaged_sources: [notes]\nsources: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ingest_script,
        "create_storage_engine",
        lambda *args, **kwargs: pytest.fail("invalid prune must not create an engine"),
    )

    with pytest.raises(SystemExit, match="at least one document"):
        ingest_script.main(
            ["--manifest", str(manifest), "--apply", "--prune-managed"]
        )
