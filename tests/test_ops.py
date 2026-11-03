"""Health monitoring, setup wizard, and other day-to-day plumbing."""
from datetime import datetime, timedelta

import pytest

from duo_tracker.config import load
from duo_tracker.health import Health


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, title, message, **kw):
        self.sent.append(title)
        return True


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.dt = datetime(2026, 11, 3, 8, 0)

    def advance(self, minutes):
        self.t += minutes * 60
        self.dt += timedelta(minutes=minutes)


def make(clock, **kw):
    notes, pings = Notes(), []
    h = Health(notes, clock=lambda: clock.t, now=lambda: clock.dt, http_get=pings.append, **kw)
    return h, notes, pings


def test_stall_alerts_once_then_recovers():
    c = Clock()
    h, notes, pings = make(c, stall_minutes=10, healthcheck_url="https://hc-ping.com/abc", daily_hour=None)
    h.success()
    c.advance(9); h.tick()
    assert notes.sent == []
    h.problem("page layout changed")
    c.advance(2); h.tick(); h.tick()
    assert notes.sent == ["⚠️ Duo-tracker stalled"]
    assert pings[-1] == "https://hc-ping.com/abc/fail"
    h.success()
    assert notes.sent[-1] == "Duo-tracker recovered" and pings[-1] == "https://hc-ping.com/abc"


def test_block_backoff_does_not_double_alert():
    c = Clock()
    h, notes, _ = make(c, stall_minutes=10, daily_hour=None)
    h.already_notified("Apple blocked checks (HTTP 541)")
    c.advance(30); h.tick()
    assert notes.sent == []          # you already got "paused"
    h.success()
    assert notes.sent == ["Duo-tracker recovered"]


def test_healthcheck_ping_throttled():
    c = Clock()
    h, _, pings = make(c, healthcheck_url="https://hc/x", daily_hour=None)
    for _ in range(5):
        h.success(); c.t += 10
    assert len(pings) == 1
    c.t += 60; h.success()
    assert len(pings) == 2


def test_daily_summary_once_per_day():
    c = Clock()  # 08:00
    h, notes, _ = make(c, daily_hour=9)
    h.success(); h.drop()
    h.tick(); assert notes.sent == []
    c.advance(61); h.success(); h.tick(); h.tick()  # 09:01
    assert notes.sent == ["Duo-tracker daily check-in"] and h.checks == 0
    c.advance(60 * 5); h.success(); h.tick()   # later same day: nothing
    assert len(notes.sent) == 1
    c.advance(60 * 24); h.success(); h.tick()  # next day, 14:01
    assert notes.sent.count("Duo-tracker daily check-in") == 2


def test_started_after_summary_hour_waits_for_tomorrow():
    c = Clock(); c.dt = datetime(2026, 11, 3, 15, 0)
    h, notes, _ = make(c, daily_hour=9)
    h.tick()
    assert notes.sent == []


def test_health_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models: [256gb-nightsky]\nhealth: {daily_summary_hour: null, stall_minutes: 1, "
                 "healthcheck_url: 'https://hc-ping.com/x'}\nsignin_check_minutes: 5\n")
    cfg = load(p)
    assert cfg.daily_summary_hour is None and cfg.stall_minutes == 3 and cfg.signin_check_minutes == 15
    assert cfg.healthcheck_url == "https://hc-ping.com/x"
    p.write_text("models: [256gb-nightsky]\nhealth: {daily_summary_hour: 25}\n")
    with pytest.raises(ValueError, match="0–23"):
        load(p)


def test_init_wizard_writes_loadable_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from duo_tracker import cli
    sent = []
    monkeypatch.setattr(cli, "cmd_notify_test", lambda args: sent.append(args.config) or 0)
    answers = iter(["1234", "90210",  # bad ZIP re-asked; a ZIP with no preset
                    "3,9,2",          # invalid (9) -> re-asked
                    "3,2",            # 512gb-nightsky, 256gb-starwhite
                    "sprint", "verizon",
                    "yolo", "submit",
                    "abc", "15",
                    "Fifth Avenue, SoHo",
                    "",               # accept generated topic
                    "https://hc-ping.com/xyz",
                    "y"])
    path = tmp_path / "config.yaml"
    assert cli.cmd_init(SimpleNamespace(config=str(path)), input_fn=lambda _: next(answers)) == 0
    cfg = load(path)
    assert [m.key for m in cfg.models] == ["512gb-nightsky", "256gb-starwhite"]
    assert cfg.carrier == "VERIZON_IPHONEDUO" and cfg.checkout_mode == "submit"
    assert cfg.max_miles == 15 and cfg.store_filter == ["fifth avenue", "soho"]
    assert cfg.ntfy_topic.startswith("duo-") and len(cfg.ntfy_topic) == 14
    assert cfg.healthcheck_url == "https://hc-ping.com/xyz" and cfg.daily_summary_hour == 9
    assert sent == [str(path)]


def test_init_wizard_keeps_existing_config(tmp_path):
    from types import SimpleNamespace

    from duo_tracker import cli
    path = tmp_path / "config.yaml"
    path.write_text("keep me")
    cli.cmd_init(SimpleNamespace(config=str(path)), input_fn=lambda _: "")
    assert path.read_text() == "keep me"


def test_headless_linux_without_screen_gives_clear_error(monkeypatch, tmp_path):
    pytest.importorskip("playwright")
    from duo_tracker import browser
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("models: [256gb-nightsky]\n")
    with pytest.raises(RuntimeError, match="no screen"):
        browser.open_context(None, load(p))


def test_exit_code_for_bad_config_prevents_restart_loop(tmp_path):
    from duo_tracker.cli import EXIT_NO_RESTART, main
    assert main(["-c", str(tmp_path / "missing.yaml"), "watch"]) == EXIT_NO_RESTART == 3
