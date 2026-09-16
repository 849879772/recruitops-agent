"""Verify an explicit Codex CLI path and its App Server command surface."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


EXPECTED_VERSION = "0.149.0"
DEFAULT_TIMEOUT_SECONDS = 10.0
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
CommandRunner = Callable[..., Any]


def _display_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _extract_version(output: str) -> str | None:
    match = _VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def _run_probe(
    command: Path,
    arguments: Sequence[str],
    *,
    timeout: float,
    runner: CommandRunner,
) -> tuple[dict[str, Any], str]:
    try:
        completed = runner(
            [str(command), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "error": "timeout"}, ""
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "error": "command_not_found"}, ""
    except PermissionError:
        return {"ok": False, "returncode": None, "error": "command_not_executable"}, ""
    except OSError:
        return {"ok": False, "returncode": None, "error": "command_execution_failed"}, ""

    return_code = getattr(completed, "returncode", None)
    result: dict[str, Any] = {
        "ok": return_code == 0,
        "returncode": return_code,
    }
    if not result["ok"]:
        result["error"] = "nonzero_exit"

    stdout = getattr(completed, "stdout", "")
    stderr = getattr(completed, "stderr", "")
    text_output = stdout if isinstance(stdout, str) else ""
    if isinstance(stderr, str):
        text_output = f"{text_output}\n{stderr}"
    return result, text_output


def verify_codex_app_server(
    command: Path | str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Run version and App Server help probes without exposing child output."""

    command_path = Path(command).expanduser()
    result: dict[str, Any] = {
        "ok": False,
        "healthy": False,
        "command": _display_path(command_path),
        "expected_version": EXPECTED_VERSION,
        "checks": {},
    }
    if timeout <= 0:
        result["error"] = "invalid_timeout"
        return result

    command_runner = runner or subprocess.run
    version_check, version_output = _run_probe(
        command_path,
        ("--version",),
        timeout=timeout,
        runner=command_runner,
    )
    observed_version = _extract_version(version_output)
    version_check["observed_version"] = observed_version
    if version_check["ok"] and observed_version != EXPECTED_VERSION:
        version_check["ok"] = False
        version_check["error"] = (
            "version_not_found" if observed_version is None else "unexpected_version"
        )

    help_check, _ = _run_probe(
        command_path,
        ("app-server", "--help"),
        timeout=timeout,
        runner=command_runner,
    )
    result["checks"] = {
        "version": version_check,
        "app_server_help": help_check,
    }
    result["ok"] = bool(version_check["ok"] and help_check["ok"])
    result["healthy"] = result["ok"]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command",
        "--codex-command",
        dest="command",
        required=True,
        type=Path,
        help="Explicit path to the installed Codex launcher, including codex.cmd on Windows.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Timeout per probe in seconds (default: %(default)s).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    result = verify_codex_app_server(args.command, timeout=args.timeout, runner=runner)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
