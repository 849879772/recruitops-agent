"""Create a conservatively slimmed copy of a sealed desktop runtime."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.desktop_runtime.resources import Bundle
from packages.desktop_runtime.staging import digest


REMOVED_DISTRIBUTIONS = {
    "colorama",
    "fastembed",
    "filelock",
    "flatbuffers",
    "fsspec",
    "hf-xet",
    "httpcore",
    "httpx",
    "huggingface-hub",
    "jieba",
    "loguru",
    "mmh3",
    "numpy",
    "onnxruntime",
    "packaging",
    "pillow",
    "protobuf",
    "py-rust-stemmers",
    "rank-bm25",
    "tokenizers",
    "tqdm",
    "win32-setctime",
}


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes runtime: {resolved}")
    return resolved


def remove_path(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_dir():
        files = [item for item in path.rglob("*") if item.is_file()]
        size = sum(item.stat().st_size for item in files)
        shutil.rmtree(path)
        return len(files), size
    size = path.stat().st_size
    path.unlink()
    return 1, size


def remove_distributions(site: Path, reference_site: Path) -> tuple[int, int, list[str]]:
    python_root = site.parents[1]
    files_removed = 0
    bytes_removed = 0
    removed: list[str] = []
    for distribution in metadata.distributions(path=[str(reference_site)]):
        name = canonical_name(distribution.metadata.get("Name", ""))
        if name not in REMOVED_DISTRIBUTIONS:
            continue
        removed.append(name)
        for relative in distribution.files or ():
            target = inside(python_root, site / relative)
            if target.is_file():
                bytes_removed += target.stat().st_size
                target.unlink()
                files_removed += 1
    for directory in sorted(
        (item for item in site.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    missing = REMOVED_DISTRIBUTIONS - set(removed)
    if missing:
        raise ValueError(f"expected distributions are missing: {sorted(missing)}")
    return files_removed, bytes_removed, sorted(removed)


def retain_locales(directory: Path) -> tuple[int, int]:
    files_removed = 0
    bytes_removed = 0
    for locale in directory.glob("*.pak"):
        if locale.name in {"en-US.pak", "zh-CN.pak"}:
            continue
        count, size = remove_path(locale)
        files_removed += count
        bytes_removed += size
    return files_removed, bytes_removed


def rebuild_manifest(runtime: Path, manifest: dict) -> None:
    files: dict[str, str] = {}
    for filename in sorted(item for item in runtime.rglob("*") if item.is_file()):
        relative = filename.relative_to(runtime).as_posix()
        if relative == "runtime-manifest.json":
            continue
        files[relative] = digest(filename)
    manifest["files"] = files
    target = runtime / "runtime-manifest.json"
    target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    Bundle.load(runtime)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume-incomplete", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        raise ValueError(f"source runtime does not exist: {source}")
    if output.exists() and not args.resume_incomplete:
        raise ValueError(f"output already exists: {output}")
    if source == output or source in output.parents:
        raise ValueError("output must not be inside the source runtime")

    manifest = Bundle.load(source).manifest
    if output.exists():
        output_manifest = json.loads(
            (output / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        for key in ("schema", "platform", "postgres_major", "components", "entrypoints"):
            if output_manifest.get(key) != manifest.get(key):
                raise ValueError("incomplete output does not match the source runtime")
    else:
        shutil.copytree(source, output)

    shutil.copyfile(
        output / "python" / "vcruntime140.dll",
        output / "postgres" / "bin" / "vcruntime140.dll",
    )

    files_removed, bytes_removed, distributions = remove_distributions(
        output / "python" / "Lib" / "site-packages",
        source / "python" / "Lib" / "site-packages",
    )
    removals = [
        output / "postgres" / "doc",
        output / "postgres" / "include",
        output / "codex" / "codex-app-server-x86_64-pc-windows-msvc.exe",
    ]
    for target in removals:
        count, size = remove_path(target)
        files_removed += count
        bytes_removed += size

    count, size = retain_locales(
        output / "chromium" / "chromium-1208" / "chrome-win64" / "locales"
    )
    files_removed += count
    bytes_removed += size

    for cache in list((output / "app").rglob("__pycache__")):
        count, size = remove_path(cache)
        files_removed += count
        bytes_removed += size

    provenance_path = output / "provenance" / "python-licenses.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance = [
        item
        for item in provenance
        if canonical_name(item.get("name", "")) not in REMOVED_DISTRIBUTIONS
    ]
    provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    rebuild_manifest(output, manifest)
    print(
        json.dumps(
            {
                "runtime": str(output),
                "files_removed": files_removed,
                "removed_mib": round(bytes_removed / 1024 / 1024, 1),
                "removed_distributions": distributions,
                "manifest_files": len(manifest["files"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
