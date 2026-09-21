"""Pinned ZIP inputs and reproducible runtime assembly, never a release signer."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import stat
import urllib.parse
import urllib.request
import zipfile

from . import RuntimeFailure
from .instance import reject_links
from .resources import Bundle, inside


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise RuntimeFailure("public_https_source_required")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise RuntimeFailure("public_https_source_required")
    return url


def fetch_archive(spec, cache):
    url = public_url(spec["source"])
    expected = spec["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeFailure("pinned_archive_hash_required")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (expected + ".zip")
    if target.exists():
        if digest(target) != expected:
            raise RuntimeFailure("cached_archive_hash_mismatch")
        return target
    # Explicit proxy, never inherited personal proxy settings. No loopback downloads.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({
        "http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}))
    temporary = target.with_suffix(".partial")
    try:
        with opener.open(url, timeout=90) as source, temporary.open("xb") as output:
            public_url(source.geturl())
            size = 0
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024**3:
                    raise RuntimeFailure("archive_too_large")
                output.write(chunk)
        if digest(temporary) != expected:
            raise RuntimeFailure("archive_hash_mismatch")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def extract_archive(archive, spec, destination):
    """Validate the entire ZIP before extracting; never execute vendor installers."""
    prefix = spec.get("strip_prefix", "")
    if prefix and not prefix.endswith("/"):
        raise RuntimeFailure("invalid_archive_prefix")
    destination.mkdir(parents=True, exist_ok=False)
    seen, planned, size = set(), [], 0
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            inside(destination, item.filename.rstrip("/"))
            if stat.S_ISLNK(item.external_attr >> 16):
                raise RuntimeFailure("linked_archive_member")
            if not item.filename.startswith(prefix):
                raise RuntimeFailure("unexpected_archive_prefix")
            relative = item.filename[len(prefix):].rstrip("/")
            if not relative:
                continue
            path = inside(destination, relative)
            if relative.casefold() in seen:
                raise RuntimeFailure("duplicate_archive_member")
            seen.add(relative.casefold())
            size += item.file_size
            if size > 4 * 1024**3 or len(seen) > 200000:
                raise RuntimeFailure("expanded_archive_too_large")
            planned.append((item, path))
        for item, path in planned:
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as src, path.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    license_path = inside(destination, spec["license_file"])
    if not license_path.is_file() or license_path.stat().st_size == 0:
        raise RuntimeFailure("missing_component_license")


def stage_components(lock, cache, destination):
    if lock.get("schema") != 1 or lock.get("platform") != "windows-x64":
        raise RuntimeFailure("unsupported_staging_lock")
    destination.mkdir(parents=True, exist_ok=False)
    records = {}
    for name, spec in lock["components"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise RuntimeFailure("invalid_component_name")
        archive = fetch_archive(spec, cache)
        target = destination / name
        extract_archive(archive, spec, target)
        records[name] = {**spec, "files": {
            p.relative_to(target).as_posix(): digest(p) for p in sorted(target.rglob("*")) if p.is_file()}}
    inventory = {"schema": 1, "platform": "windows-x64", "release_accepted": False, "components": records}
    (destination / "staging-inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return inventory


def assemble_bundle(plan, inputs, destination):
    """Explicit hashed file allowlist only: never recursively copy a checkout/home."""
    inputs = inputs.resolve()
    reject_links(inputs)
    if destination.exists() or destination.resolve().is_relative_to(inputs):
        raise RuntimeFailure("fresh_separate_bundle_required")
    mappings = plan["copy"]
    seen = set()
    for mapping in mappings:
        source = inside(inputs, mapping["source"])
        inside(destination.resolve(), mapping["target"])
        if mapping["target"].casefold() in seen or mapping["target"] == "runtime-manifest.json":
            raise RuntimeFailure("duplicate_bundle_target")
        seen.add(mapping["target"].casefold())
        if digest(source) != mapping["sha256"]:
            raise RuntimeFailure("staging_input_hash_mismatch")
    destination.mkdir(parents=True)
    files = {}
    for mapping in mappings:
        target = inside(destination.resolve(), mapping["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inside(inputs, mapping["source"]), target)
        files[mapping["target"]] = mapping["sha256"]
    manifest = {"schema": 1, "platform": "windows-x64", "postgres_major": 16,
                "components": plan["components"], "entrypoints": plan["entrypoints"], "files": files}
    # An incomplete staging tree deliberately has no runtime-manifest.json.
    Bundle(destination.resolve(), manifest).verify()
    (destination / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def package_bundle(root, target):
    bundle = Bundle.load(root)
    if target.exists() or target.resolve().is_relative_to(bundle.root):
        raise RuntimeFailure("fresh_separate_archive_required")
    temporary = target.with_suffix(".partial")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as output:
            for relative in sorted([*bundle.manifest["files"], "runtime-manifest.json"]):
                info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                output.writestr(info, inside(bundle.root, relative).read_bytes())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"sha256": digest(target), "release_accepted": False}
