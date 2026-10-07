from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.resources import Bundle
from packages.desktop_runtime.staging import digest
from scripts.desktop import promote_sources as promotion
from test_desktop_runtime import bundle


@pytest.fixture
def install(tmp_path, bundle, monkeypatch):
    """All writes stay in pytest's fixture tree, never an actual installation."""
    repo = tmp_path / "repository"
    formal = repo / "artifacts/releases/RecruitOps"
    runtime = formal / promotion.RUNTIME
    runtime.parent.mkdir(parents=True)
    shutil.move(bundle.root, runtime)
    manifest = bundle.manifest
    for relative, content in {
        "apps/api/main.py": b"old_api = True\n",
        "packages/automation/worker.py": b"old_worker = True\n",
        "apps/web/app.js": b"const oldWeb = true;\n",
        "pyproject.toml": b"[project]\nversion = '1.0.0'\n",
    }.items():
        target = runtime / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest["files"]["app/" + relative] = digest(target)
    sources = sorted(p[4:] for p in manifest["files"] if p.startswith("app/"))
    for relative in sources:
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runtime / "app" / relative, source)
    provenance = runtime / "provenance/application-files.json"
    provenance.parent.mkdir()
    provenance.write_bytes(promotion.json_bytes(sources))
    manifest["files"]["provenance/application-files.json"] = digest(provenance)
    (runtime / "runtime-manifest.json").write_bytes(promotion.json_bytes(manifest))
    data = formal / ".data/database"
    data.mkdir(parents=True)
    (data / "user-records.db").write_bytes(b"private fixture data")
    monkeypatch.setattr(promotion, "running_processes", lambda root: [])
    monkeypatch.setattr(promotion, "source_inventory", lambda repository, runtime: sorted(
        relative for relative in sources if (repository / relative).is_file()) + sorted(
        p.relative_to(repository).as_posix() for p in (repository / "packages").rglob("*.py")
        if p.relative_to(repository).as_posix() not in sources))
    Bundle.load(runtime)
    return repo, formal, runtime, repo / "artifacts/releases/source-backup"


def changed_install(install):
    repo, formal, runtime, backup = install
    (repo / "apps/api/main.py").write_bytes(b"new_api = True\n")
    new = repo / "packages/automation/startup/new.py"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"startup = True\n")
    return promotion.make_plan(repo)


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_plan_is_read_only_and_supports_new_business_module(install):
    repo, formal, runtime, backup = install
    original = tree_bytes(formal)
    plan = changed_install(install)
    assert tree_bytes(formal) == original
    assert not backup.exists()
    assert len(plan["changes"]) == 4
    assert plan["changes"][1]["old_sha256"] is None
    assert [c["path"] for c in plan["changes"][-2:]] == [promotion.PROVENANCE, promotion.MANIFEST]
    assert plan["backup_bytes"] == sum(len(original[c["path"]]) for c in plan["changes"] if c["old_sha256"])


def test_apply_copies_only_replaced_files_and_manifests_and_preserves_data(install, monkeypatch):
    repo, formal, runtime, backup = install
    original = tree_bytes(formal)
    data_before = promotion.data_inventory(formal)
    plan = changed_install(install)
    writes = []
    atomic = promotion._atomic_bytes

    def record(target, content, expected):
        if target.is_relative_to(formal):
            writes.append(target.relative_to(formal).as_posix())
        return atomic(target, content, expected)

    monkeypatch.setattr(promotion, "_atomic_bytes", record)
    report = promotion.apply_plan(plan, backup)
    assert report["status"] == "completed"
    assert report["data_unchanged"] is True
    assert writes == [c["path"] for c in plan["changes"]]
    assert promotion.data_inventory(formal) == data_before
    assert not (backup / ".data").exists()
    saved = tree_bytes(backup)
    assert set(saved) == {c["path"] for c in plan["changes"] if c["old_sha256"]} | {promotion.REPORT}
    for change in plan["changes"]:
        assert digest(formal / change["path"]) == change["new_sha256"]
        if change["old_sha256"]:
            assert saved[change["path"]] == original[change["path"]]
    Bundle.load(runtime)


def test_failure_restores_prior_files_and_removes_new_module_and_empty_dirs(install, monkeypatch):
    repo, formal, runtime, backup = install
    original = tree_bytes(formal)
    plan = changed_install(install)
    atomic = promotion._atomic_bytes
    failed = False

    def fail_once(target, content, expected):
        nonlocal failed
        if target == formal / promotion.MANIFEST and not failed:
            failed = True
            raise OSError("fixture replacement failure")
        return atomic(target, content, expected)

    monkeypatch.setattr(promotion, "_atomic_bytes", fail_once)
    with pytest.raises(OSError, match="fixture replacement failure"):
        promotion.apply_plan(plan, backup)
    assert tree_bytes(formal) == original
    assert not (runtime / "app/packages/automation/startup").exists()
    assert json.loads((backup / promotion.REPORT).read_text())["status"] == "rolled_back"
    Bundle.load(runtime)


def test_persisted_backup_can_be_rolled_back(install):
    repo, formal, runtime, backup = install
    original = tree_bytes(formal)
    promotion.apply_plan(changed_install(install), backup)
    report = promotion.rollback_backup(repo, backup)
    assert report["status"] == "rolled_back"
    assert tree_bytes(formal) == original


@pytest.mark.parametrize("relative", ["pyproject.toml", "migrations/001_test.sql", "packages/desktop_runtime/api_bootstrap.py", "scripts/apply_migrations.py"])
def test_dependency_migration_and_runtime_changes_require_full_packaging(install, relative):
    repo, formal, runtime, backup = install
    (repo / relative).write_bytes(b"changed")
    with pytest.raises(ValueError, match="requires full desktop packaging"):
        promotion.make_plan(repo)
    assert not backup.exists()


def test_removed_source_requires_full_packaging(install):
    repo, formal, runtime, backup = install
    (repo / "apps/api/main.py").unlink()
    with pytest.raises(ValueError, match="removed sources require full desktop packaging"):
        promotion.make_plan(repo)


@pytest.mark.parametrize("relative", ["apps/desktop/src/main.ts", "packages/desktop_browser/index.cjs", "migrations/002_new.sql", "pyproject.toml"])
def test_shell_and_dependency_paths_are_not_business_sources(relative):
    assert not promotion.business_source(relative)


def test_old_installation_integrity_is_required(install):
    repo, formal, runtime, backup = install
    (runtime / "app/apps/api/main.py").write_bytes(b"unrecorded change")
    with pytest.raises(RuntimeFailure, match="hash_mismatch"):
        promotion.make_plan(repo)


def test_source_changed_after_plan_is_rejected_before_backup(install):
    repo, formal, runtime, backup = install
    plan = changed_install(install)
    original = tree_bytes(formal)
    (repo / "apps/api/main.py").write_bytes(b"changed after planning")
    with pytest.raises(ValueError, match="source changed after planning"):
        promotion.apply_plan(plan, backup)
    assert tree_bytes(formal) == original
    assert not backup.exists()


def test_running_formal_blocks_apply_without_stopping_or_writing(install, monkeypatch):
    repo, formal, runtime, backup = install
    plan = changed_install(install)
    original = tree_bytes(formal)
    monkeypatch.setattr(promotion, "running_processes", lambda root: [{"ProcessId": 123}])
    with pytest.raises(RuntimeError, match="exit normally first"):
        promotion.apply_plan(plan, backup)
    assert tree_bytes(formal) == original
    assert not backup.exists()


@pytest.mark.parametrize("code, expected", [(0, []), (10, []), (259, [{"ProcessId": 123}])])
def test_only_native_still_active_process_rows_block(monkeypatch, code, expected):
    monkeypatch.setattr(promotion, "_scan_processes", lambda root: [{"ProcessId": 123}])
    monkeypatch.setattr(promotion, "_process_exit_code", lambda pid: code)
    assert promotion.running_processes(Path("fixture")) == expected


def test_process_query_failure_is_not_treated_as_exit(monkeypatch):
    monkeypatch.setattr(promotion, "_scan_processes", lambda root: [{"ProcessId": 123}])

    def fail(pid):
        raise OSError("access denied")

    monkeypatch.setattr(promotion, "_process_exit_code", fail)
    with pytest.raises(OSError, match="access denied"):
        promotion.running_processes(Path("fixture"))


@pytest.mark.parametrize("relative", ["../outside", "C:/outside", "safe/file:ads", "safe\\file", ".data/../app.py", "safe/NUL"])
def test_traversal_and_unsafe_windows_paths_are_rejected(tmp_path, relative):
    with pytest.raises(RuntimeFailure, match="unsafe_resource_path"):
        promotion.child(tmp_path, relative)


def test_formal_and_backup_are_restricted_to_exact_installation(install):
    repo, formal, runtime, backup = install
    with pytest.raises(ValueError, match="only repository"):
        promotion.formal_path(repo, formal / ".data")
    for bad in (formal, formal / ".data/backup", formal.parent, repo / "backup"):
        with pytest.raises(ValueError, match="backup must"):
            promotion.backup_path(repo, formal, bad)


def test_reparse_ancestor_is_rejected_without_following_it(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available")
    with pytest.raises(ValueError, match="linked path rejected"):
        promotion.child(tmp_path, "linked/new.py")
    assert not (target / "new.py").exists()


def test_dangling_reparse_ancestor_is_checked_without_exists(tmp_path, monkeypatch):
    linked = tmp_path / "dangling-junction"
    original = Path.lstat

    def linked_stat(path):
        if path == linked:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", linked_stat)
    with pytest.raises(ValueError, match="linked path rejected"):
        promotion.plain_path(linked / "new.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path inventory")
def test_data_inventory_handles_long_cache_paths_without_opening_data(install):
    repo, formal, runtime, backup = install
    long_directory = promotion.extended_path(formal / ".data") / ("a" * 150) / ("b" * 150)
    long_directory.mkdir(parents=True)
    (long_directory / "checkpoint.bin").write_bytes(b"fixture")
    result = promotion.data_inventory(formal)
    assert result["files"] == 2
    assert result["bytes"] == len(b"private fixture data") + len(b"fixture")


def test_rollback_refuses_modified_backup_or_target(install):
    repo, formal, runtime, backup = install
    plan = changed_install(install)
    promotion.apply_plan(plan, backup)
    (runtime / "app/apps/api/main.py").write_bytes(b"later user change")
    before = tree_bytes(formal)
    with pytest.raises(ValueError, match="rollback target has changed"):
        promotion.rollback_backup(repo, backup)
    assert tree_bytes(formal) == before


def test_rollback_cannot_remove_user_data_directory_from_tampered_report(install):
    repo, formal, runtime, backup = install
    promotion.apply_plan(changed_install(install), backup)
    empty = formal / ".data/empty"
    empty.mkdir()
    report_path = backup / promotion.REPORT
    report = json.loads(report_path.read_text())
    report["created_directories"] = [".data/empty"]
    report["data"] = promotion.data_inventory(formal)
    report_path.write_bytes(promotion.json_bytes(report))
    before = tree_bytes(formal)
    with pytest.raises(ValueError, match="invalid rollback directory"):
        promotion.rollback_backup(repo, backup)
    assert empty.is_dir()
    assert tree_bytes(formal) == before


@pytest.mark.parametrize("relative", [promotion.PROVENANCE, promotion.MANIFEST])
def test_rollback_requires_original_manifest_backups(install, relative):
    repo, formal, runtime, backup = install
    promotion.apply_plan(changed_install(install), backup)
    report_path = backup / promotion.REPORT
    report = json.loads(report_path.read_text())
    for change in report["changes"]:
        if change["path"] == relative:
            change["old_sha256"] = None
    report_path.write_bytes(promotion.json_bytes(report))
    before = tree_bytes(formal)
    with pytest.raises(ValueError, match="manifest backups are required"):
        promotion.rollback_backup(repo, backup)
    assert tree_bytes(formal) == before


def test_rollback_refuses_directory_in_place_of_new_module(install):
    repo, formal, runtime, backup = install
    promotion.apply_plan(changed_install(install), backup)
    new = runtime / "app/packages/automation/startup/new.py"
    new.unlink()
    new.mkdir()
    with pytest.raises(ValueError, match="rollback target is not a file"):
        promotion.rollback_backup(repo, backup)
    assert new.is_dir()


def test_rollback_checks_processes_immediately_before_directory_cleanup(install, monkeypatch):
    repo, formal, runtime, backup = install
    plan = changed_install(install)
    promotion.apply_plan(plan, backup)
    checks = 0

    def started_before_cleanup(root):
        nonlocal checks
        checks += 1
        if checks == len(plan["changes"]) + 2:
            raise RuntimeError("fixture process started before directory cleanup")

    monkeypatch.setattr(promotion, "assert_stopped", started_before_cleanup)
    with pytest.raises(RuntimeError, match="started before directory cleanup"):
        promotion.rollback_backup(repo, backup)
    assert (runtime / "app/packages/automation/startup").is_dir()


def test_rollback_rechecks_each_current_hash_after_preflight(install, monkeypatch):
    repo, formal, runtime, backup = install
    promotion.apply_plan(changed_install(install), backup)
    checks = 0
    target = runtime / "app/apps/api/main.py"

    def interfere(root):
        nonlocal checks
        checks += 1
        if checks == 2:
            target.write_bytes(b"concurrent external edit")

    monkeypatch.setattr(promotion, "assert_stopped", interfere)
    with pytest.raises(ValueError, match="rollback target has changed"):
        promotion.rollback_backup(repo, backup)
    assert target.read_bytes() == b"concurrent external edit"


def test_cli_defaults_to_plan_and_requires_explicit_apply_for_rollback(install, capsys):
    repo, formal, runtime, backup = install
    original = tree_bytes(formal)
    changed_install(install)
    assert promotion.main(["--repository", str(repo)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["applied"] is False
    assert output["ready_to_apply"] is True
    assert tree_bytes(formal) == original
    with pytest.raises(SystemExit):
        promotion.main(["--repository", str(repo), "--rollback", str(backup)])
    with pytest.raises(SystemExit):
        promotion.main(["--repository", str(repo), "--apply"])
