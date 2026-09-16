"""Explicit, one-request vision smoke check using a synthetic (non-private) image."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from packages.config import get_settings
from packages.matching.client import _default_transport, DeepSeekClientError
from packages.vision import VisionError, VisionService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", action="store_true")
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 520, "height": 240})
        page.set_content('<main style="font:28px Arial;padding:25px;background:white;color:black">'
                         '<h2>Record VX73-Q9</h2><p>Job: Software Engineer</p><p>Status: Applied</p></main>')
        picture = page.screenshot()
        browser.close()
    settings = get_settings()
    payload = {
        "model": args.model, "stream": False, "max_tokens": 180,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Read the record reference, job title and application status visible in this image. Reply briefly. Do not infer missing content."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(picture).decode()}},
        ]}],
    }
    result = {"model": args.model, "image_source": "synthetic_local_page", "requests": 1}
    try:
        if args.service:
            reading = VisionService(api_key=settings.llm_api_key, model=args.model).analyze(
                "data:image/png;base64," + base64.b64encode(picture).decode())
            result.update({"success": True, **reading.model_dump()})
        else:
            raw = _default_transport("https://api.deepseek.com/chat/completions", {
                "Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"
            }, payload, 35)
            result.update({"success": True, "returned_model": raw.get("model"), "choices": raw.get("choices"), "usage": raw.get("usage")})
    except (DeepSeekClientError, VisionError) as exc:
        result.update({"success": False, "error": exc.code})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
