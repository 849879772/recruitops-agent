"""Inspect package metadata only; never import application settings or connect."""

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
import tomllib

from packaging.requirements import Requirement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="check root application requirements as well")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    requirements = [line for line in Path(__file__).with_name("test-requirements.txt").read_text().splitlines() if line and not line.startswith("#")]
    if args.full:
        requirements += tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    missing, incompatible, installed = [], [], {}
    for spec in requirements:
        requirement = Requirement(spec)
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement.name)
            continue
        installed[requirement.name] = version
        if version not in requirement.specifier:
            incompatible.append(spec)
    print(json.dumps({"python": sys.executable, "scope": "full" if args.full else "deterministic",
                      "missing": sorted(set(missing)), "incompatible": incompatible,
                      "installed": installed, "ready": not (missing or incompatible)}, indent=2))
    return 2 if missing or incompatible else 0


if __name__ == "__main__":
    raise SystemExit(main())
