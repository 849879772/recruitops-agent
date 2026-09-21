"""Acquire public native build inputs into the repository's isolated build area."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import inside

INPUTS = {
    "python": ("3.11.9", "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip", "", "LICENSE.txt"),
    "postgres": ("16.15", "https://get.enterprisedb.com/postgresql/postgresql-16.15-4-windows-x64-binaries.zip", "pgsql/", "server_license.txt"),
    "pgvector": ("0.8.1", "https://codeload.github.com/pgvector/pgvector/zip/refs/tags/v0.8.1", "pgvector-0.8.1/", "LICENSE"),
}
EXPECTED = {
    "python": "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b",
    "postgres": "f5f55b03bd54ce0dd1c51d524b54c7e015abd4d620af27d6971288a2dbe4a8f8",
    "pgvector": "61182a6afd6fb94c0e7740c037a7d3c36a8cab1cc581ae5bd9b7b2cfd789bdfc",
}


def main():
    root = ROOT / ".desktop-runtime-tests/native-build"
    root.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({
        "http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}))
    ledger = {}
    for name, (version, url, prefix, license_file) in INPUTS.items():
        archive = root / f"{name}-{version}.zip"
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            with opener.open(url, timeout=120) as src, partial.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            partial.replace(archive)
        with archive.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != EXPECTED[name]:
            raise ValueError("pinned native archive hash mismatch")
        target = root / name
        if not target.exists():
            target.mkdir()
            with zipfile.ZipFile(archive) as src:
                for item in src.infolist():
                    if not item.filename.startswith(prefix):
                        raise ValueError("unexpected archive prefix")
                    relative = item.filename[len(prefix):].rstrip("/")
                    if not relative:
                        continue
                    # EDB also carries pgAdmin and stackbuilder; neither is runtime.
                    if name == "postgres" and relative.split("/")[0] not in {"bin", "lib", "share", "include", "doc", "server_license.txt", "commandlinetools_3rd_party_licenses.txt"}:
                        continue
                    path = inside(target.resolve(), relative)
                    if item.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with src.open(item) as source, path.open("xb") as output:
                            shutil.copyfileobj(source, output)
        ledger[name] = {"version": version, "source": url, "sha256": checksum,
                        "checksum_kind": "observed_official_https_download_not_publisher_attestation",
                        "license_file": license_file, "archive_bytes": archive.stat().st_size}
        (root / "acquisition.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        print(json.dumps({"component": name, **ledger[name]}), flush=True)


if __name__ == "__main__":
    main()
