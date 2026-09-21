"""Create a flat portable ZIP and report its Windows extraction path budget."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        raise ValueError(f"portable source does not exist: {source}")
    if output.exists():
        raise ValueError(f"portable output already exists: {output}")

    files = sorted(item for item in source.rglob("*") if item.is_file())
    if any(item.is_symlink() for item in source.rglob("*")):
        raise ValueError("portable source contains a symbolic link")
    relative_names = [item.relative_to(source).as_posix() for item in files]
    required = {
        "RecruitOps-Desktop-Preview.exe",
        "README-PORTABLE.txt",
        "resources/app.asar",
        "resources/desktop-runtime/runtime-manifest.json",
        "resources/desktop-filler/manifest.json",
    }
    missing = required - set(relative_names)
    if missing:
        raise ValueError(f"portable source is incomplete: {sorted(missing)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for filename, relative in zip(files, relative_names, strict=True):
            archive.write(filename, relative)

    with zipfile.ZipFile(output) as archive:
        corrupt = archive.testzip()
        names = set(archive.namelist())
    if corrupt or required - names:
        output.unlink(missing_ok=True)
        raise ValueError(f"portable ZIP verification failed: {corrupt or sorted(required - names)}")

    digest = hashlib.file_digest(output.open("rb"), "sha256").hexdigest().upper()
    checksum = output.with_suffix(output.suffix + ".sha256.txt")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    longest = max(map(len, relative_names))
    print(
        json.dumps(
            {
                "output": str(output),
                "size_mib": round(output.stat().st_size / 1024 / 1024, 1),
                "entries": len(relative_names),
                "longest_relative_path": longest,
                "maximum_extraction_root_for_259": 258 - longest,
                "sha256": digest,
            }
        )
    )


if __name__ == "__main__":
    main()
