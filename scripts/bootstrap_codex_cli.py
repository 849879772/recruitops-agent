"""Install and check the pinned standalone Codex CLI package."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "@openai/codex"
EXPECTED_VERSION = "0.149.0"
DEFAULT_INSTALL_DIR = ROOT / ".data" / "codex-cli"
DEFAULT_INSTALL_TIMEOUT_SECONDS = 180.0

CommandRunner = Callable[..., Any]


def _display_path(path: Path) -> str:
    return str(path.resolve(strict=False))


def resolve_codex_command(
    install_dir: Path | str,
    *,
    platform_name: str | None = None,
) -> Path | None:
    """Return the npm launcher, including the Windows ``.cmd`` launcher."""

    target = Path(install_dir).expanduser()
    platform = (platform_name or os.name).casefold()
    if platform in {"nt", "win32", "windows"}:
        native_candidates = (
            target
            / "node_modules"
            / "@openai"
            / package
            / "vendor"
            / architecture
            / "bin"
            / "codex.exe"
            for package, architecture in (
                ("codex-win32-x64", "x86_64-pc-windows-msvc"),
                ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
            )
        )
        for candidate in native_candidates:
            if candidate.is_file():
                return candidate
        names = ("codex.cmd", "codex.exe", "codex")
    else:
        names = ("codex", "codex.cmd")

    bin_dir = target / "node_modules" / ".bin"
    for name in names:
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    return None


def _read_package_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing_package_manifest"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid_package_manifest"
    if not isinstance(value, dict):
        return None, "invalid_package_manifest"
    return value, None


def check_installation(install_dir: Path | str) -> dict[str, Any]:
    """Check only the local package manifest and npm launcher files."""

    target = Path(install_dir).expanduser().resolve(strict=False)
    manifest_path = target / "node_modules" / "@openai" / "codex" / "package.json"
    manifest, manifest_error = _read_package_manifest(manifest_path)

    package_check: dict[str, Any] = {
        "ok": False,
        "expected_name": PACKAGE_NAME,
        "expected_version": EXPECTED_VERSION,
    }
    if manifest is None:
        package_check["error"] = manifest_error or "invalid_package_manifest"
    else:
        installed_name = manifest.get("name")
        installed_version = manifest.get("version")
        package_check["installed_name"] = (
            installed_name if isinstance(installed_name, str) else None
        )
        package_check["installed_version"] = (
            installed_version if isinstance(installed_version, str) else None
        )
        package_check["ok"] = (
            installed_name == PACKAGE_NAME and installed_version == EXPECTED_VERSION
        )
        if not package_check["ok"]:
            package_check["error"] = "package_mismatch"

    command_path = resolve_codex_command(target)
    command_check: dict[str, Any] = {"ok": command_path is not None}
    if command_path is None:
        command_check["error"] = "missing_codex_launcher"

    return {
        "ok": bool(package_check["ok"] and command_check["ok"]),
        "package": PACKAGE_NAME,
        "expected_version": EXPECTED_VERSION,
        "install_dir": _display_path(target),
        "command": _display_path(command_path) if command_path else None,
        "checks": {
            "package": package_check,
            "command": command_check,
        },
    }


def _npm_command(platform_name: str | None = None) -> str:
    platform = (platform_name or os.name).casefold()
    return "npm.cmd" if platform in {"nt", "win32", "windows"} else "npm"


def install_cli(
    install_dir: Path | str,
    *,
    timeout: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Install the exact package into an isolated npm prefix."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")

    target = Path(install_dir).expanduser().resolve(strict=False)
    current = check_installation(target)
    if current["ok"]:
        current["action"] = "already_installed"
        return current

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        current["action"] = "install_failed"
        current["error"] = "install_directory_unavailable"
        return current

    npm_args = [
        _npm_command(),
        "install",
        "--prefix",
        str(target),
        "--no-save",
        "--no-package-lock",
        "--no-fund",
        "--no-audit",
        f"{PACKAGE_NAME}@{EXPECTED_VERSION}",
    ]
    command_runner = runner or subprocess.run
    try:
        completed = command_runner(
            npm_args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        current["action"] = "install_failed"
        current["error"] = "npm_install_timeout"
        return current
    except FileNotFoundError:
        current["action"] = "install_failed"
        current["error"] = "npm_not_found"
        return current
    except OSError:
        current["action"] = "install_failed"
        current["error"] = "npm_execution_failed"
        return current

    return_code = getattr(completed, "returncode", None)
    if return_code != 0:
        current["action"] = "install_failed"
        current["error"] = "npm_install_failed"
        current["npm_returncode"] = return_code
        return current

    result = check_installation(target)
    result["action"] = "installed"
    result["npm_returncode"] = return_code
    if not result["ok"]:
        result["error"] = "post_install_check_failed"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check the local pinned installation.")
    mode.add_argument("--install", action="store_true", help="Install the pinned npm package.")
    parser.add_argument(
        "--install-dir",
        "--target-dir",
        dest="install_dir",
        type=Path,
        default=DEFAULT_INSTALL_DIR,
        help="Isolated npm prefix (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_INSTALL_TIMEOUT_SECONDS,
        help="npm install timeout in seconds (default: %(default)s).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    target = args.install_dir.expanduser().resolve(strict=False)
    try:
        if args.install:
            result = install_cli(target, timeout=args.timeout, runner=runner)
        elif args.timeout <= 0:
            result = {
                "ok": False,
                "error": "invalid_timeout",
                "install_dir": _display_path(target),
            }
        else:
            result = check_installation(target)
    except ValueError:
        result = {
            "ok": False,
            "error": "invalid_timeout",
            "install_dir": _display_path(target),
        }

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
