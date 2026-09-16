"""Disposable worker for fixture-only crawler candidate execution."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

from packages.recruitment_core.crawlers.declarative import DeclarativeRecruitCrawler


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("network and child processes are disabled during fixture acceptance")


def _disable_side_channels() -> None:
    socket.create_connection = _blocked
    socket.socket.connect = _blocked
    subprocess.Popen = _blocked
    subprocess.run = _blocked
    try:
        import requests

        requests.sessions.Session.request = _blocked
    except ImportError:  # pragma: no cover - project dependency is normally installed
        pass


def _fixture_json_requester(fixture: dict[str, Any]):
    responses = fixture.get("responses")
    if not isinstance(responses, list):
        raise ValueError("declarative API fixture requires a responses list")
    remaining = [dict(item) for item in responses if isinstance(item, dict)]

    def request(_request: dict[str, Any], body: dict[str, Any]) -> Any:
        for index, item in enumerate(remaining):
            match = item.get("match") or {}
            matched = isinstance(match, dict) and all(
                body.get(key) == value for key, value in match.items()
            )
            if matched:
                return remaining.pop(index).get("payload")
        raise ValueError(f"fixture has no response for request body {body!r}")

    return request


def _fixture_page_renderer(fixture: dict[str, Any]):
    pages = fixture.get("pages")
    if not isinstance(pages, dict):
        raise ValueError("declarative DOM fixture requires a pages object")

    def render(url: str, **_kwargs: Any) -> str:
        if url not in pages:
            raise ValueError(f"fixture has no HTML page for {url}")
        return str(pages[url])

    return render


def _run_declarative(request: dict[str, Any]) -> dict[str, Any]:
    fixture = request.get("fixture") or {}
    recipe = request.get("recipe") or {}
    recipe_type = recipe.get("type")
    crawler = DeclarativeRecruitCrawler(
        str(request["company"]),
        str(request["source_url"]),
        recipe,
        json_requester=(
            _fixture_json_requester(fixture) if recipe_type == "api_campaigns" else None
        ),
        page_renderer=(
            _fixture_page_renderer(fixture)
            if recipe_type in {"dom", "html_list"}
            else None
        ),
    )
    jobs = crawler.fetch()
    return {
        "jobs": jobs,
        "pagination_complete": crawler.pagination_complete,
        "pages_seen": int(fixture.get("pages_seen") or 1),
        "total_pages": fixture.get("total_pages", 1),
        "has_more": bool(fixture.get("has_more", False)),
        "advertised_total": crawler.expected_total,
    }


def _run_python(request: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(request.get("python_file") or "")).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != request.get("source_sha256"):
        raise ValueError("python candidate changed after acceptance request was created")
    module_spec = importlib.util.spec_from_file_location("recruitops_isolated_candidate", path)
    if module_spec is None or module_spec.loader is None:
        raise ValueError("unable to load python candidate")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    parser = getattr(module, "parse_fixture", None)
    if not callable(parser):
        raise ValueError("python candidate must define parse_fixture(fixture)")
    result = parser(dict(request.get("fixture") or {}))
    if isinstance(result, list):
        result = {
            "jobs": result,
            "pagination_complete": True,
            "pages_seen": 1,
            "total_pages": 1,
            "has_more": False,
            "advertised_total": len(result),
        }
    if not isinstance(result, dict):
        raise ValueError("parse_fixture must return a jobs list or evidence object")
    return result


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("candidate request must be an object")
        _disable_side_channels()
        if request.get("kind") == "declarative":
            result = _run_declarative(request)
        elif request.get("kind") == "python":
            result = _run_python(request)
        else:
            raise ValueError("unsupported candidate kind")
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[-1_000:]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
