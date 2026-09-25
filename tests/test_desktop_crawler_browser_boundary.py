from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from packages.desktop_runtime.isolation import child_environment
from packages.recruitment_core.crawlers.base import launch_browser


def test_child_environment_launches_manifest_chromium(tmp_path, monkeypatch):
    root = tmp_path / "bundle space \u4e2d\u6587"
    executable = root / "chromium/custom-layout/chrome.exe"
    bundle = SimpleNamespace(
        root=root,
        resource=lambda name: executable if name == "chromium" else root / name,
    )
    monkeypatch.setenv("RECRUITOPS_BROWSER_EXECUTABLE_PATH", "personal/edge.exe")
    monkeypatch.setenv("RECRUITOPS_BROWSER_CHANNEL", "msedge")
    env = child_environment(
        SimpleNamespace(data=tmp_path / "isolated"), bundle, 55001, 55002, "test", "test"
    )
    assert env["RECRUITOPS_BROWSER_EXECUTABLE_PATH"] == str(executable)
    assert env["RECRUITOPS_BROWSER_CHANNEL"] == "chromium"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(root / "chromium")
    browser = Mock()
    with patch.dict("os.environ", env, clear=True):
        result = launch_browser(
            browser, channel="msedge", executable_path="other.exe", headless=True,
            args=["--disable-gpu"],
        )
    browser.chromium.launch.assert_called_once_with(
        executable_path=str(executable), headless=True, args=["--disable-gpu"]
    )
    assert result is browser.chromium.launch.return_value
    result.close()


def test_desktop_missing_executable_fails_without_fallback():
    browser = Mock()
    with patch.dict("os.environ", {"RECRUITOPS_ENV": "desktop-isolated"}, clear=True):
        with pytest.raises(RuntimeError, match="desktop_browser_executable_missing"):
            launch_browser(browser, headless=True)
    browser.chromium.launch.assert_not_called()


@pytest.mark.parametrize("env,kwargs,expected", [
    ({}, {}, {"channel": "msedge"}),
    ({"RECRUITOPS_BROWSER_CHANNEL": "chrome"}, {}, {"channel": "chrome"}),
    ({"RECRUITOPS_BROWSER_CHANNEL": "chromium"}, {}, {}),
    ({"RECRUITOPS_BROWSER_CHANNEL": "bundled"}, {}, {}),
    ({"RECRUITOPS_BROWSER_CHANNEL": ""}, {"channel": "chrome"}, {"channel": "chrome"}),
    ({"RECRUITOPS_BROWSER_EXECUTABLE_PATH": "custom.exe"}, {},
     {"executable_path": "custom.exe"}),
    ({"RECRUITOPS_BROWSER_EXECUTABLE_PATH": "custom.exe"}, {"channel": "chrome"},
     {"executable_path": "custom.exe", "channel": "chrome"}),
])
def test_non_desktop_browser_configuration_unchanged(env, kwargs, expected):
    browser = Mock()
    with patch.dict("os.environ", env, clear=True):
        launch_browser(browser, headless=True, **kwargs)
    browser.chromium.launch.assert_called_once_with(headless=True, **expected)
    browser.chromium.launch.return_value.close()
