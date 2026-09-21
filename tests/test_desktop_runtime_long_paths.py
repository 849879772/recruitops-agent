"""Real disposable long Windows paths; never inspect a user instance."""

import os
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.instance import extended_path, reject_links
from test_desktop_runtime import bundle, supervisor_fixture
from test_desktop_runtime_persistence import reopen


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path IO")


def deep_cache(root):
    path = root / "codex/.tmp/plugins/plugins/synthetic-plugin/skills/synthetic-plugin"
    for _ in range(5):
        path /= "synthetic-support-directory"
    path /= "references/synthetic-software/valuation-rules.md"
    assert len(str(path)) > 300
    extended = extended_path(path)
    extended.parent.mkdir(parents=True)
    extended.write_text("synthetic only", encoding="utf-8")
    return path


def test_real_long_cache_survives_owned_instance_restart(tmp_path, bundle):
    first, tree, _ = supervisor_fixture(tmp_path, bundle)
    first.start()
    identity = first.events.instance_id
    first.stop()
    leaf = deep_cache(first.layout.data)
    reject_links(first.layout.data)
    second = reopen(first, tree)
    second.start()
    try:
        assert second.events.instance_id == identity
        assert extended_path(leaf).read_text(encoding="utf-8") == "synthetic only"
    finally:
        second.stop()


def test_real_long_path_junction_is_rejected(tmp_path):
    import _winapi

    root = tmp_path / "instance"
    leaf = deep_cache(root)
    target = tmp_path / "other-fixture"
    target.mkdir()
    junction = extended_path(leaf.parent / "forbidden-junction")
    _winapi.CreateJunction(str(target), str(junction))
    try:
        with pytest.raises(RuntimeFailure, match="linked_instance"):
            reject_links(root)
        with pytest.raises(RuntimeFailure, match="linked_instance"):
            reject_links(junction)
    finally:
        # rmdir removes only this junction, never its target directory.
        junction.rmdir()
    assert target.is_dir()


def test_real_long_path_symlink_is_rejected(tmp_path):
    leaf = deep_cache(tmp_path / "instance")
    link = extended_path(leaf.parent / "forbidden-symlink")
    try:
        link.symlink_to(extended_path(leaf))
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable; junction test remains mandatory")
        raise
    with pytest.raises(RuntimeFailure, match="linked_instance"):
        reject_links(tmp_path / "instance")


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_scan_does_not_suppress_stat_errors(tmp_path, monkeypatch, error):
    leaf = extended_path(deep_cache(tmp_path / "instance"))
    original = Path.lstat

    def fail(path, *args, **kwargs):
        if path == leaf:
            raise error("synthetic scan failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail)
    with pytest.raises(error, match="synthetic scan failure"):
        reject_links(tmp_path / "instance")


def test_extended_path_preserves_unc_and_is_idempotent():
    path = Path(r"\\server.invalid\share\fixture")
    assert str(extended_path(path)) == r"\\?\UNC\server.invalid\share\fixture"
    assert extended_path(extended_path(path)) == extended_path(path)


def test_symlink_stat_is_rejected_even_without_reparse_attribute(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFLNK, st_file_attributes=0))
    with pytest.raises(RuntimeFailure, match="linked_instance"):
        reject_links(tmp_path)


def test_scan_does_not_suppress_enumeration_errors(tmp_path, monkeypatch):
    root = extended_path(tmp_path)
    original = Path.iterdir

    def fail(path):
        if path == root:
            raise PermissionError("synthetic enumeration failure")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", fail)
    with pytest.raises(PermissionError, match="synthetic enumeration failure"):
        reject_links(tmp_path)
