"""Resolve public application dependencies to a repository-local wheelhouse."""

import json
from pathlib import Path
import subprocess
import sys
import tomllib
import hashlib
import os

ROOT = Path(__file__).resolve().parents[2]


def main():
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    destination = ROOT / ".desktop-runtime-tests/native-build/wheels"
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / "tmp"
    temporary.mkdir(exist_ok=True)
    environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary))
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--index-url", "https://pypi.org/simple",
                    "--no-cache-dir", "--wheel-dir", str(destination), *dependencies, "websockets==15.0.1"], check=True, env=environment)
    records = []
    for path in sorted(destination.glob("*.whl")):
        with path.open("rb") as stream:
            records.append({"filename": path.name, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()})
    (destination.parent / "wheels.json").write_text(json.dumps({"index": "https://pypi.org/simple",
        "requirements": dependencies + ["websockets==15.0.1"], "wheels": records}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
