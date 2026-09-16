"""Render a read-only weekly observability report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from packages.config import Settings
from packages.observability.weekly import (
    aggregate_weekly_observability,
    render_weekly_markdown,
    report_to_json,
)
from packages.storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of UTC calendar days to include (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON/Markdown file or output stem; writes both formats when provided.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Agent PostgreSQL/SQLite URL; defaults to RECRUITOPS_DATABASE_URL.",
    )
    parser.add_argument(
        "--telemetry-path",
        type=Path,
        default=None,
        help="Codex JSONL trace path; defaults to RECRUITOPS_CODEX_TRACE_PATH.",
    )
    return parser


def _configured_path(path: Path, settings: Settings) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else settings.agent_root / expanded


def _output_paths(output: Path) -> tuple[Path, Path]:
    if output.exists() and output.is_dir():
        return output / "weekly-observability.json", output / "weekly-observability.md"
    if output.suffix.casefold() == ".json":
        return output, output.with_suffix(".md")
    if output.suffix.casefold() in {".md", ".markdown"}:
        return output.with_suffix(".json"), output
    return Path(f"{output}.json"), Path(f"{output}.md")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    settings = Settings()
    database_url = args.database_url or settings.database_url
    telemetry_path = args.telemetry_path
    if telemetry_path is None:
        telemetry_path = _configured_path(settings.codex_trace_path, settings)

    storage = Storage.from_url(database_url)
    try:
        report = aggregate_weekly_observability(
            storage,
            days=args.days,
            telemetry_path=telemetry_path,
        )
    finally:
        storage.engine.dispose()

    serialized = report_to_json(report)
    markdown = render_weekly_markdown(report)
    if args.output is not None:
        json_path, markdown_path = _output_paths(args.output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(serialized + "\n", encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")

    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
