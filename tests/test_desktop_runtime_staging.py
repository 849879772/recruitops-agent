import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

from packages.desktop_runtime import RuntimeFailure
from packages.desktop_runtime.staging import (
    assemble_bundle, digest, extract_archive, fetch_archive, package_bundle, public_url,
)
from test_desktop_runtime import bundle


@pytest.mark.parametrize("url", ["http://vendor.test/x", "https://localhost/x", "https://user:pass@vendor.test/x", "https://vendor.test/x?token=secret"])
def test_source_rejects_credentials_and_non_public_urls(url):
    with pytest.raises(RuntimeFailure, match="public_https_source_required"):
        public_url(url)


def archive(tmp_path, entries):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w") as output:
        for name, content in entries:
            output.writestr(name, content)
    return path


@pytest.mark.parametrize("name", ["../escape", "C:/escape", "safe/file:ads"])
def test_zip_traversal_rejected_before_copy(tmp_path, name):
    path = archive(tmp_path, [("safe/LICENSE", b"license"), (name, b"x")])
    with pytest.raises(RuntimeFailure, match="unsafe_resource_path"):
        extract_archive(path, {"strip_prefix": "safe/", "license_file": "LICENSE"}, tmp_path / "stage")
    assert not list((tmp_path / "stage").iterdir())


def test_zip_links_and_case_collisions_rejected(tmp_path):
    info = zipfile.ZipInfo("safe/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = archive(tmp_path, [(info, b"../../outside")])
    with pytest.raises(RuntimeFailure, match="linked_archive_member"):
        extract_archive(path, {"strip_prefix": "safe/", "license_file": "LICENSE"}, tmp_path / "linked")
    path = archive(tmp_path, [("safe/LICENSE", b"l"), ("safe/license", b"l")])
    with pytest.raises(RuntimeFailure, match="duplicate_archive_member"):
        extract_archive(path, {"strip_prefix": "safe/", "license_file": "LICENSE"}, tmp_path / "collision")


def test_cached_archive_hash_required_without_network(tmp_path):
    content = b"fixture"
    expected = hashlib.sha256(content).hexdigest()
    cached = tmp_path / (expected + ".zip")
    cached.write_bytes(content)
    spec = {"source": "https://example.invalid/fixture.zip", "sha256": expected}
    assert fetch_archive(spec, tmp_path) == cached
    cached.write_bytes(b"corrupt")
    with pytest.raises(RuntimeFailure, match="cached_archive_hash_mismatch"):
        fetch_archive(spec, tmp_path)


def test_assemble_and_reproducible_package_fixture_only(tmp_path, bundle):
    plan = {"components": bundle.manifest["components"], "entrypoints": bundle.manifest["entrypoints"],
            "copy": [{"source": name, "target": name, "sha256": value}
                     for name, value in bundle.manifest["files"].items()]}
    target = tmp_path / "assembled"
    manifest = assemble_bundle(plan, bundle.root, target)
    assert manifest["files"] == bundle.manifest["files"]
    first = package_bundle(target, tmp_path / "first.zip")
    second = package_bundle(target, tmp_path / "second.zip")
    assert first == second and first["release_accepted"] is False


def test_incomplete_plan_never_writes_launchable_manifest(tmp_path, bundle):
    plan = {"components": {}, "entrypoints": {}, "copy": []}
    target = tmp_path / "incomplete"
    with pytest.raises(RuntimeFailure, match="incomplete_manifest"):
        assemble_bundle(plan, bundle.root, target)
    assert not (target / "runtime-manifest.json").exists()


def test_assembly_refuses_tampered_input_before_creating_destination(tmp_path, bundle):
    relative = next(iter(bundle.manifest["files"]))
    plan = {"copy": [{"source": relative, "target": relative, "sha256": "0" * 64}]}
    target = tmp_path / "tampered"
    with pytest.raises(RuntimeFailure, match="staging_input_hash_mismatch"):
        assemble_bundle(plan, bundle.root, target)
    assert not target.exists()
