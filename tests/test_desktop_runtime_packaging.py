from pathlib import Path

import pytest

from packages.desktop_runtime.__main__ import main
from packages.desktop_runtime.isolation import child_environment
from packages.desktop_runtime.resources import Layout
from test_desktop_runtime import bundle


@pytest.mark.parametrize("name", ["user-data", "private", ".desktop-runtime-tests-wrong"])
def test_packaged_bootstrap_cannot_broaden_isolation_root(tmp_path, bundle, capsys, name):
    root = tmp_path / name
    assert main(["--resources", str(bundle.root), "--start", "--instance", str(root / "instance"),
                 "--isolation-root", str(root)]) == 2
    assert "invalid_isolation_root" in capsys.readouterr().out
    assert not root.exists()


def test_packaged_browser_environment_uses_revision_install_root(tmp_path, bundle):
    env = child_environment(Layout(bundle.root, tmp_path / "instance"), bundle, 55001, 55002, "fixture", "fixture")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(bundle.root / "chromium")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] != str(bundle.resource("chromium").parent)


def test_embedded_python_paths_are_bundle_relative_not_ambient():
    root = Path(__file__).resolve().parents[1]
    paths = (root / "scripts/desktop/python311._pth").read_text().splitlines()
    assert paths == ["python311.zip", ".", "Lib/site-packages", "../app", "import site"]


def test_native_inputs_are_pinned_to_observed_official_archives():
    from scripts.desktop.acquire_native import EXPECTED, INPUTS
    assert set(EXPECTED) == set(INPUTS) == {"python", "postgres", "pgvector"}
    assert all(len(digest) == 64 for digest in EXPECTED.values())
    assert all(value[1].startswith("https://") for value in INPUTS.values())
