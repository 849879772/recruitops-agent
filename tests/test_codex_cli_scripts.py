from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts import bootstrap_codex_cli, verify_codex_app_server


def _write_fake_installation(target: Path) -> Path:
    package_dir = target / "node_modules" / "@openai" / "codex"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        json.dumps(
            {
                "name": bootstrap_codex_cli.PACKAGE_NAME,
                "version": bootstrap_codex_cli.EXPECTED_VERSION,
            }
        ),
        encoding="utf-8",
    )
    launcher_name = "codex.cmd" if bootstrap_codex_cli.os.name == "nt" else "codex"
    launcher = target / "node_modules" / ".bin" / launcher_name
    launcher.parent.mkdir(parents=True)
    launcher.write_text("fake launcher", encoding="utf-8")
    return launcher


def test_windows_launcher_resolution_uses_cmd_as_fallback(tmp_path: Path) -> None:
    launcher = tmp_path / "node_modules" / ".bin" / "codex.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")

    assert (
        bootstrap_codex_cli.resolve_codex_command(tmp_path, platform_name="nt")
        == launcher
    )


def test_windows_launcher_resolution_prefers_native_binary(tmp_path: Path) -> None:
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    shim = tmp_path / "node_modules" / ".bin" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("shim", encoding="utf-8")

    assert bootstrap_codex_cli.resolve_codex_command(tmp_path, platform_name="nt") == native


def test_check_requires_exact_package_version_and_launcher(tmp_path: Path) -> None:
    launcher = _write_fake_installation(tmp_path)

    result = bootstrap_codex_cli.check_installation(tmp_path)

    assert result["ok"] is True
    assert Path(result["command"]) == launcher.resolve()
    assert result["checks"]["package"]["installed_version"] == "0.149.0"

    manifest = launcher.parents[1] / "@openai" / "codex" / "package.json"
    manifest.write_text('{"name": "@openai/codex", "version": "0.149.1"}', encoding="utf-8")
    assert bootstrap_codex_cli.check_installation(tmp_path)["ok"] is False


def test_install_pins_package_and_does_not_print_npm_output(tmp_path: Path, capsys) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        target = Path(argv[argv.index("--prefix") + 1])
        _write_fake_installation(target)
        return SimpleNamespace(returncode=0, stdout="SECRET_KEY=should-not-print", stderr="SECRET")

    exit_code = bootstrap_codex_cli.main(
        ["--install", "--install-dir", str(tmp_path), "--timeout", "3"],
        runner=fake_runner,
    )

    assert exit_code == 0
    assert calls[0][0][-1] == "@openai/codex@0.149.0"
    assert "--no-package-lock" in calls[0][0]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 3
    assert "SECRET" not in capsys.readouterr().out


def test_verify_runs_explicit_commands_with_timeout_without_child_output() -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.149.0\nSECRET_KEY=hidden",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="app-server help SECRET", stderr="")

    result = verify_codex_app_server.verify_codex_app_server(
        "C:/isolated/node_modules/.bin/codex.cmd",
        timeout=2.5,
        runner=fake_runner,
    )

    assert result["ok"] is True
    assert result["healthy"] is True
    assert [call[0][1:] for call in calls] == [["--version"], ["app-server", "--help"]]
    assert all(call[1]["timeout"] == 2.5 for call in calls)
    assert "SECRET" not in json.dumps(result)


def test_verify_main_prints_json_health_result(capsys) -> None:
    def fake_runner(argv, **kwargs):
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="codex 0.149.0", stderr="")
        return SimpleNamespace(returncode=0, stdout="usage: app-server", stderr="")

    exit_code = verify_codex_app_server.main(
        ["--command", "C:/isolated/node_modules/.bin/codex.cmd"],
        runner=fake_runner,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["healthy"] is True
    assert payload["checks"]["app_server_help"]["ok"] is True


def test_verify_reports_timeout_and_still_checks_help() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "--version":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="usage: app-server", stderr="")

    result = verify_codex_app_server.verify_codex_app_server(
        "C:/isolated/node_modules/.bin/codex.cmd",
        runner=fake_runner,
    )

    assert result["ok"] is False
    assert result["checks"]["version"]["error"] == "timeout"
    assert result["checks"]["app_server_help"]["ok"] is True
    assert len(calls) == 2
