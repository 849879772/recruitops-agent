"""Opt-in local bundle acceptance; no crawling, model calls or desktop windows."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from packages.desktop_runtime.isolation import child_environment
from packages.desktop_runtime.resources import Bundle, Layout


@pytest.mark.skipif(
    os.environ.get("RECRUITOPS_TEST_BUNDLED_CHROMIUM") != "1",
    reason="explicit opt-in required for local headless bundle acceptance",
)
def test_new_child_environment_launches_actual_bundle_offline(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".desktop-runtime-tests/native-without-personal-knowledge"
    bundle = Bundle(root.resolve(), json.loads(
        (root / "runtime-manifest.json").read_text(encoding="utf-8")
    ))
    assert bundle.resource("chromium").is_relative_to(root.resolve() / "chromium")
    assert bundle.resource("chromium").is_file()
    data = tmp_path / "new isolated child"
    for folder in ("home", "tmp"):
        (data / folder).mkdir(parents=True)
    env = child_environment(Layout(bundle.root, data), bundle, 55001, 55002, "test", "test")
    script = r'''
import json
import sys
sys.path.insert(0, sys.argv[1])
from playwright.sync_api import sync_playwright
from packages.recruitment_core.crawlers.base import launch_browser
with sync_playwright() as pw:
    browser = launch_browser(pw, headless=True, args=[
        "--disable-background-networking", "--disable-component-update",
        "--disable-sync", "--no-first-run", "--no-default-browser-check",
        "--disable-domain-reliability", "--disable-breakpad",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
    ])
    try:
        context = browser.new_context(offline=True, service_workers="block")
        context.route("**/*", lambda route: route.abort())
        requests = []
        context.on("request", lambda request: requests.append(request.url))
        page = context.new_page()
        page.goto("about:blank")
        assert page.evaluate("1 + 1") == 2
        assert page.url == "about:blank"
        assert requests == []
        print(json.dumps({"url": page.url, "requests": requests, "version": browser.version}))
        context.close()
    finally:
        browser.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo)], env=env, cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip())
    assert receipt["url"] == "about:blank"
    assert receipt["requests"] == []
    assert receipt["version"]
