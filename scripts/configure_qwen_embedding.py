"""Generate local service credentials; activate only after authenticated GPU health succeeds."""
import argparse
import json
import getpass
import os
from pathlib import Path
import secrets
import subprocess
from urllib.request import Request, urlopen

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
MODEL = "Qwen/Qwen3-Embedding-0.6B"
REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    directory = ROOT / ".data/embedding"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "service.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "enabled": False, "api_key": secrets.token_urlsafe(48), "port": 8015,
        "model": MODEL, "model_revision": REVISION, "model_path": str(directory / "model"),
    }
    if args.activate:
        request = Request(f"http://127.0.0.1:{config['port']}/health", headers={"Authorization": "Bearer " + config["api_key"]})
        with urlopen(request, timeout=10) as response:
            health = json.load(response)
        if not health.get("ready") or health.get("model") != MODEL or health.get("device") != "cuda":
            raise RuntimeError("Qwen GPU health verification failed; configuration unchanged")
        changes = {"RECRUITOPS_KNOWLEDGE_EMBEDDING_ENDPOINT": f"http://host.docker.internal:{config['port']}/v1/embeddings",
                   "RECRUITOPS_KNOWLEDGE_EMBEDDING_API_KEY": config["api_key"],
                   "RECRUITOPS_KNOWLEDGE_EMBEDDING_MODEL": MODEL, "RECRUITOPS_KNOWLEDGE_EMBEDDING_DIMENSION": "1024"}
        env_path = ROOT / ".env"
        backup = directory / "previous-embedding-config.json"
        if not backup.exists():
            previous = dotenv_values(env_path)
            backup.write_text(json.dumps({key: previous.get(key) for key in changes}), encoding="utf-8")
        for key, value in changes.items():
            set_key(str(env_path), key, value, quote_mode="always")
        config["enabled"] = True
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name == "nt":
        for private in (path, directory / "previous-embedding-config.json"):
            if private.exists():
                subprocess.run(["icacls", str(private), "/inheritance:r", "/grant:r", getpass.getuser() + ":(F)", "*S-1-5-18:(F)"], check=True, capture_output=True)
    print("Qwen GPU configuration activated" if args.activate else "Local service configuration prepared; project model unchanged")


if __name__ == "__main__":
    main()
