"""Linux-readiness of the browser launch: sandbox fallback, evasion args intact.

These don't launch a browser; they capture the kwargs open_context would use on
each platform and assert the anti-bot evasion stack is present and unchanged.
"""
import sys
from types import SimpleNamespace

import pytest

pw_api = pytest.importorskip("playwright.sync_api")
from duo_tracker import browser  # noqa: E402


def _capture(monkeypatch, platform, userns_enabled, tmp_path):
    seen = {}

    class FakeCtx:
        def cookies(self):
            return []

    class FakeChromium:
        @staticmethod
        def launch_persistent_context(**kw):
            seen.update(kw)
            return FakeCtx()

    fake_pw = SimpleNamespace(chromium=FakeChromium())
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(browser, "_userns_clones_enabled", lambda: userns_enabled)
    monkeypatch.setenv("DISPLAY", ":99")
    cfg = SimpleNamespace(profile_dir=tmp_path / "prof", browser_channel="chromium",
                          schedule_tz="America/New_York")
    cfg.profile_dir.mkdir()
    browser.open_context(fake_pw, cfg)
    return seen


def test_linux_keeps_sandbox_and_evasion_when_userns_allowed(monkeypatch, tmp_path):
    k = _capture(monkeypatch, "linux", userns_enabled=True, tmp_path=tmp_path)
    assert k["chromium_sandbox"] is True
    # the evasion tells must all still be there
    assert "--start-maximized" not in k["args"]
    assert k["args"] == ["--disable-blink-features=AutomationControlled"]
    assert k["ignore_default_args"] == ["--enable-automation"]
    assert k["headless"] is False
    assert k["no_viewport"] is True
    assert k["user_agent"] == browser.REAL_UA


def test_linux_drops_sandbox_only_when_kernel_forbids_userns(monkeypatch, tmp_path):
    k = _capture(monkeypatch, "linux", userns_enabled=False, tmp_path=tmp_path)
    assert k["chromium_sandbox"] is False


def test_macos_launch_is_unchanged(monkeypatch, tmp_path):
    # Even with the (Linux-only) userns knob 'off', macOS keeps the sandbox on
    # and uses the exact same args as before this change.
    k = _capture(monkeypatch, "darwin", userns_enabled=False, tmp_path=tmp_path)
    assert k["chromium_sandbox"] is True
    assert k["args"] == ["--disable-blink-features=AutomationControlled"]
    assert k["ignore_default_args"] == ["--enable-automation"]
    assert k["user_agent"] == browser.REAL_UA
