"""Resumable bounded range download of official, SHA256-verified GPU/model artifacts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urldefrag, urljoin
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[1] / ".data/embedding"
CHUNK = 16 * 1024 * 1024


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", choices=["torch", "model"])
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=6)
    args = parser.parse_args()

    def opener():
        return build_opener(ProxyHandler({"http": args.proxy, "https": args.proxy}))

    if args.artifact == "model":
        url = "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/model.safetensors"
        digest = "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd"
        size = 1191586416
        destination = ROOT / "model/model.safetensors"
    else:
        index = "https://download.pytorch.org/whl/cu128/torch/"
        with opener().open(index, timeout=40) as response:
            links = Links()
            links.feed(response.read().decode())
        filename = "torch-2.8.0+cu128-cp311-cp311-win_amd64.whl"
        matches = [urljoin(index, x) for x in links.links if unquote(urldefrag(x)[0]).endswith("/" + filename)]
        if len(matches) != 1:
            raise RuntimeError("Cannot identify official torch wheel")
        url, fragment = urldefrag(matches[0])
        url = url.replace("https://download.pytorch.org/", "https://download-r2.pytorch.org/")
        if not fragment.startswith("sha256="):
            raise RuntimeError("Missing official SHA256")
        digest = fragment.split("=", 1)[1]
        size = 3461420395
        destination = ROOT / filename
        print(f"Official wheel SHA256: {digest}; source: {url}", flush=True)
    if destination.exists() and destination.stat().st_size == size:
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                print(args.artifact + " already verified")
                return
    parts = ROOT / "downloads" / args.artifact
    parts.mkdir(parents=True, exist_ok=True)

    def download(number):
        start, end = number * CHUNK, min(size, (number + 1) * CHUNK) - 1
        path = parts / str(number)
        expected = end - start + 1
        if path.exists() and path.stat().st_size == expected:
            return expected
        for attempt in range(4):
            try:
                if args.artifact == "torch":
                    header = path.with_suffix(".headers")
                    subprocess.run(["curl.exe", "--silent", "--show-error", "--fail", "--connect-timeout", "20",
                                    "--max-time", "180", "--max-filesize", str(expected), "--proxy", args.proxy,
                                    "--range", f"{start}-{end}", "--dump-header", str(header), "--output", str(path), url],
                                   check=True, capture_output=True, timeout=190)
                    ranges = re.findall(r"(?im)^content-range:\s*(.+)$", header.read_text())
                    if not ranges or ranges[-1].strip() != f"bytes {start}-{end}/{size}" or path.stat().st_size != expected:
                        raise RuntimeError("Invalid torch byte range")
                    return expected
                request = Request(url, headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity", "User-Agent": "curl/8.12.1"})
                with opener().open(request, timeout=60) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise RuntimeError("Server did not honor byte range")
                    with path.open("wb") as stream:
                        remaining = expected
                        while remaining:
                            block = response.read(min(1024 * 1024, remaining))
                            if not block:
                                raise RuntimeError("Incomplete range")
                            stream.write(block)
                            remaining -= len(block)
                return expected
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

    numbers = range((size + CHUNK - 1) // CHUNK)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        completed = 0
        for length in pool.map(download, numbers):
            completed += length
            print(f"{args.artifact}: {completed // (1024 * 1024)} / {size // (1024 * 1024)} MiB", flush=True)
    temporary = destination.with_suffix(destination.suffix + ".verified.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    with temporary.open("wb") as stream:
        for number in numbers:
            with (parts / str(number)).open("rb") as part:
                while block := part.read(1024 * 1024):
                    checksum.update(block)
                    stream.write(block)
    if checksum.hexdigest() != digest:
        raise RuntimeError("SHA256 mismatch; final artifact was not replaced")
    os.replace(temporary, destination)
    print(f"{args.artifact}: SHA256 verified", flush=True)


if __name__ == "__main__":
    main()
