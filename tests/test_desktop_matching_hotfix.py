from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.resources import Bundle
from packages.desktop_runtime.staging import digest
from scripts.desktop import apply_matching_fix_to_formal as hotfix
from test_desktop_runtime import bundle


@pytest.fixture
def installation(tmp_path, bundle):
    root = tmp_path / "repository"
    formal = root / "artifacts" / "releases" / "RecruitOps"
    runtime = formal / "resources" / "desktop-runtime"
    shutil.copytree(bundle.root, runtime)
    manifest = json.loads((runtime / hotfix.MANIFEST).read_bytes())
    for index, relative in enumerate(hotfix.FILES):
        source = root / relative
        target = runtime / "app" / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"VALUE = {index + 10}\n", encoding="utf-8")
        target.write_text(f"VALUE = {index}\n", encoding="utf-8")
        manifest["files"]["app/" + relative] = digest(target)
    provenance = runtime / hotfix.PROVENANCE
    provenance.parent.mkdir()
    provenance.write_text(json.dumps(sorted(
        name[4:] for name in manifest["files"] if name.startswith("app/")
    )), encoding="utf-8")
    manifest["files"][hotfix.PROVENANCE] = digest(provenance)
    (runtime / hotfix.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    data = formal / ".data" / "must-not-change.txt"
    data.parent.mkdir()
    data.write_bytes(b"existing user database placeholder")
    Bundle.load(runtime)
    return root, formal, runtime, data


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_default_dry_run_changes_nothing_and_lists_running_processes(installation):
    root, formal, _, _ = installation
    before = _files(root)
    processes = [{"ProcessId": 42, "Name": "postgres.exe"}]
    result = hotfix.deploy(root, process_probe=lambda path: processes if path == formal else [])
    assert result["mode"] == "dry_run"
    assert result["changed"] == list(hotfix.FILES)
    assert result["ready"] is False
    assert result["running_processes"] == processes
    assert _files(root) == before


def test_apply_blocks_live_process_before_backup_or_changes(installation):
    root, _, _, _ = installation
    before = _files(root)
    with pytest.raises(RuntimeError, match="formal_processes_running"):
        hotfix.deploy(root, apply=True, process_probe=lambda _: [{"ProcessId": 42}])
    assert _files(root) == before
    assert len(list((root / "artifacts/releases").iterdir())) == 1


def test_apply_rechecks_processes_after_backup(installation):
    root, formal, _, _ = installation
    before = _files(formal)
    answers = iter([[], [{"ProcessId": 99}]])
    with pytest.raises(RuntimeError, match="formal_processes_started_before_update"):
        hotfix.deploy(root, apply=True, process_probe=lambda _: next(answers))
    assert _files(formal) == before


def test_apply_updates_only_allowlist_manifest_and_keeps_complete_backup(installation):
    root, formal, runtime, data = installation
    before = _files(formal)
    result = hotfix.deploy(root, apply=True, process_probe=lambda _: [])
    after = _files(formal)
    changed = {name for name in before if before[name] != after[name]}
    prefix = "resources/desktop-runtime/"
    assert changed == {prefix + "app/" + relative for relative in hotfix.FILES} | {prefix + hotfix.MANIFEST}
    assert data.read_bytes() == b"existing user database placeholder"
    backup = Path(result["backup"])
    for relative in (*("app/" + item for item in hotfix.FILES), hotfix.PROVENANCE, hotfix.MANIFEST):
        assert (backup / relative).read_bytes() == before[prefix + relative]
    Bundle.load(runtime)
    second = hotfix.deploy(root, apply=True, process_probe=lambda _: [])
    assert second["already_current"] is True
    assert second["updated"] == []
    assert len(list((root / "artifacts/releases").iterdir())) == 2


def test_invalid_existing_bundle_is_rejected_before_any_write(installation):
    root, _, runtime, _ = installation
    (runtime / "app" / hotfix.FILES[0]).write_text("tampered", encoding="utf-8")
    before = _files(root)
    with pytest.raises(RuntimeFailure, match="hash_mismatch"):
        hotfix.deploy(root, apply=True, process_probe=lambda _: [])
    assert _files(root) == before


def test_partial_copy_failure_rolls_back_and_keeps_data(installation, monkeypatch):
    root, formal, runtime, _ = installation
    before = _files(formal)
    replace = hotfix._replace_bytes
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path == runtime / "app" / hotfix.FILES[1] and not failed:
            failed = True
            raise OSError("fixture copy failure")
        replace(path, content)

    monkeypatch.setattr(hotfix, "_replace_bytes", fail_once)
    with pytest.raises(OSError, match="fixture copy failure"):
        hotfix.deploy(root, apply=True, process_probe=lambda _: [])
    assert _files(formal) == before
    Bundle.load(runtime)


def test_post_update_verification_failure_rolls_back_manifest(installation, monkeypatch):
    root, formal, runtime, _ = installation
    before = _files(formal)
    original_load = hotfix.Bundle.load
    loads = 0

    def fail_second_load(path):
        nonlocal loads
        loads += 1
        if loads == 2:
            raise RuntimeError("fixture post-update failure")
        return original_load(path)

    monkeypatch.setattr(hotfix.Bundle, "load", fail_second_load)
    with pytest.raises(RuntimeError, match="fixture post-update failure"):
        hotfix.deploy(root, apply=True, process_probe=lambda _: [])
    assert _files(formal) == before
    original_load(runtime)


def test_process_query_failure_blocks_update(installation):
    root, _, _, _ = installation
    before = _files(root)

    def unavailable(_):
        raise RuntimeError("formal_process_check_failed")

    with pytest.raises(RuntimeError, match="formal_process_check_failed"):
        hotfix.deploy(root, apply=True, process_probe=unavailable)
    assert _files(root) == before


@pytest.mark.skipif(hotfix.os.name != "nt", reason="Windows process probe")
def test_windows_probe_checks_full_install_and_redacts_commandlines(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout='[{"ProcessId":42,"Name":"python.exe","ExecutablePath":"中文"}]'.encode())

    monkeypatch.setattr(hotfix.subprocess, "run", run)
    rows = hotfix.formal_processes(Path("D:/RecruitOps"))
    command, kwargs = calls[0]
    assert rows[0]["ExecutablePath"] == "中文"
    assert "Get-CimInstance Win32_Process" in command[-1]
    assert "ExecutablePath" in command[-1] and "CommandLine" in command[-1]
    assert "Select-Object ProcessId, Name, ExecutablePath" in command[-1]
    assert kwargs["env"]["RECRUITOPS_FORMAL_UPDATE_ROOT"] == str(Path("D:/RecruitOps"))
    assert kwargs["check"] and kwargs["timeout"] == 30


def test_cli_defaults_to_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(hotfix, "deploy", lambda **kwargs: calls.append(kwargs) or {"mode": "dry_run"})
    assert hotfix.main([]) == 0
    assert calls == [{"apply": False}]
    assert json.loads(capsys.readouterr().out)["mode"] == "dry_run"
