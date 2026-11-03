from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from duo_tracker.config import load
from duo_tracker.schedule import Schedule, parse_windows, summarize_blocks

ET = ZoneInfo("America/New_York")
WINDOWS = parse_windows([{"start": "07:30", "end": "09:30", "seconds": 30},
                         {"start": "23:30", "end": "00:30", "seconds": 30}])


def at(h, m, day=3):
    return datetime(2026, 11, day, h, m, tzinfo=ET)


@pytest.mark.parametrize("h,m,expect", [
    (7, 29, (90, "off-peak")), (7, 30, (30, "peak 07:30–09:30")), (9, 29, (30, "peak 07:30–09:30")),
    (9, 30, (90, "off-peak")), (23, 45, (30, "peak 23:30–00:30")), (0, 15, (30, "peak 23:30–00:30")),
    (0, 30, (90, "off-peak")), (14, 0, (90, "off-peak")),
])
def test_windows_including_midnight_wrap(h, m, expect):
    assert Schedule(90, WINDOWS).current(at(h, m)) == expect


def test_utc_input_is_converted_to_eastern():
    # 13:00 UTC on Nov 3 = 08:00 EST
    utc = datetime(2026, 11, 3, 13, 0, tzinfo=ZoneInfo("UTC"))
    assert Schedule(90, WINDOWS).current(utc)[0] == 30


def test_block_slows_only_the_rest_of_that_window(tmp_path):
    log = tmp_path / "blocks.csv"
    s = Schedule(90, WINDOWS, log_path=log)
    s.on_block("HTTP 541", at(8, 0))
    assert s.current(at(8, 1)) == (90, "peak 07:30–09:30 (slowed after a block)")
    assert s.current(at(23, 40))[0] == 30          # other window unaffected
    assert s.current(at(8, 0, day=4))[0] == 30      # tomorrow's window is fast again
    # midnight window: a block at 23:50 also covers 00:10 of the next day
    s.on_block("HTTP 541", at(23, 50))
    assert s.current(at(0, 10, day=4))[0] == 90
    rows = log.read_text().strip().splitlines()
    assert rows[0].startswith("at,reason,interval_s") and len(rows) == 3
    assert ",30,peak 07:30–09:30," in rows[1] and rows[2].endswith(",950.0")


def test_block_summary(tmp_path):
    log = tmp_path / "blocks.csv"
    assert "No blocks" in summarize_blocks(log)
    s = Schedule(90, WINDOWS, log_path=log)
    s.on_block("HTTP 541", at(8, 0))
    s.on_block("HTTP 541", at(14, 0))
    out = summarize_blocks(log)
    assert "2 blocks over 1 day" in out and "every  30s when blocked: 1×" in out and "every  90s" in out


def test_config_schedule_and_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models: [256gb-nightsky]\n")
    cfg = load(p)
    assert cfg.schedule_default == 30 and cfg.schedule_windows == ()   # old configs: flat 30s
    for bad, msg in [
        ("schedule: {default_seconds: 10}", ">= 30"),
        ("schedule: {windows: [{start: '07:30', end: '09:30', seconds: 5}]}", ">= 30"),
        ("schedule: {windows: [{start: '7am', end: '09:30'}]}", "07:30"),
        ("schedule: {timezone: Mars/Olympus}", "timezone"),
        ("zip: 1175", "5-digit"),
    ]:
        p.write_text("models: [256gb-nightsky]\n" + bad + "\n")
        with pytest.raises(ValueError, match=msg):
            load(p)


def test_example_config_is_the_11754_setup():
    from pathlib import Path
    cfg = load(Path(__file__).parent.parent / "config.example.yaml")
    assert cfg.zip == "11754" and len(cfg.models) == 4 and cfg.schedule_default == 90
    assert "walt whitman" in cfg.store_filter and "r068" in cfg.store_filter


def test_wizard_11754_preset(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from duo_tracker import cli
    monkeypatch.setattr(cli, "cmd_notify_test", lambda args: 0)
    path = tmp_path / "config.yaml"
    # accept every default: ZIP 11754 preset, unlocked, submit, preset miles/stores, generated topic, no healthcheck
    cli.cmd_init(SimpleNamespace(config=str(path)), input_fn=lambda _: "")
    cfg = load(path)
    assert cfg.zip == "11754" and cfg.checkout_mode == "submit" and cfg.max_miles == 30
    assert [m.key for m in cfg.models] == ["256gb-nightsky", "256gb-starwhite", "512gb-nightsky", "512gb-starwhite"]
    assert "smith haven" in cfg.store_filter and "r327" in cfg.store_filter
    assert [w.label for w in cfg.schedule_windows] == ["07:30–09:30", "23:30–00:30"] and cfg.schedule_default == 90


def test_store_filter_matches_preset_by_name_or_number():
    from duo_tracker.availability import Availability, StoreStock, matching_stores
    av = Availability(part="X", stores=[
        StoreStock("Walt Whitman", "R068", "Huntington Station", "9.2 mi", 9.2, True, ""),
        StoreStock("Queens Center", "R612", "Elmhurst", "33.7 mi", 33.7, True, ""),
        StoreStock("Smith Haven", "R139", "Lake Grove", "6.7 mi", 6.7, False, ""),
    ])
    pre = ["smith haven", "r139", "walt whitman", "r068", "roosevelt field", "r060", "manhasset", "r327"]
    assert [s.store_name for s in matching_stores(av, pre, 30)] == ["Walt Whitman"]


def test_watch_loop_uses_schedule_and_logs_blocks(tmp_path, monkeypatch):
    """Drive cmd_watch with a scripted browser: peak rate, then a block, then the slowed rate."""
    pytest.importorskip("playwright")
    import types

    from duo_tracker import browser, checkout, cli
    from duo_tracker.availability import Availability
    from duo_tracker.schedule import Schedule

    monkeypatch.setattr(checkout, "STATE_DIR", tmp_path / "state")
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [256gb-nightsky]\ncheckout: {mode: alert}\nhealth: {daily_summary_hour: null}\n"
                    "schedule: {default_seconds: 90, windows: [{start: '07:30', end: '09:30', seconds: 30}]}\n")

    clock = {"now": at(8, 0)}
    real_current = Schedule.current
    monkeypatch.setattr(Schedule, "current", lambda self, now=None: real_current(self, now or clock["now"]))
    real_block = Schedule.on_block
    monkeypatch.setattr(Schedule, "on_block", lambda self, r, now=None: real_block(self, r, now or clock["now"]))

    rounds = iter(["ok", "blocked", "ok"])

    def fake_round(page, models, carrier):
        r = next(rounds)
        if r == "blocked":
            raise browser.Blocked("HTTP 541")
        yield models[0], Availability(part=models[0].part, has_pickup_data=True)

    waits = []

    def fake_sleep(secs, on_tick=None):
        waits.append(secs)
        clock["now"] = clock["now"].replace(minute=clock["now"].minute + 1)
        if len(waits) == 3:
            raise KeyboardInterrupt

    ctx = types.SimpleNamespace(pages=[object()], close=lambda: None)
    monkeypatch.setattr(browser, "open_context", lambda pw, cfg: ctx)
    monkeypatch.setattr(browser, "check_round", fake_round)
    monkeypatch.setattr(browser, "sleep_with_status", fake_sleep)
    monkeypatch.setattr(cli.Notifier, "send", lambda self, *a, **k: True)

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import playwright.sync_api as sapi
    monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())

    assert cli.cmd_watch(types.SimpleNamespace(config=str(cfgp))) == 0
    # round 1 ok at peak -> 30s; block -> backoff sleep (600s); round 3 ok, window now slowed -> 90s
    assert waits == [30, 600, 90]
    log = (tmp_path / "state" / "blocks.csv").read_text()
    assert "HTTP 541,30,peak 07:30–09:30" in log


def test_stale_rehearsal_downgrades_to_review_instead_of_exiting(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    import types

    from duo_tracker import browser, checkout, cli

    monkeypatch.setattr(checkout, "STATE_DIR", tmp_path / "state")  # no rehearsal on file
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [256gb-nightsky]\ncheckout: {mode: submit}\nhealth: {daily_summary_hour: null}\n")
    seen = {}

    def fake_open(pw, cfg):
        seen["mode"] = cfg.checkout_mode
        raise KeyboardInterrupt  # stop right after startup

    monkeypatch.setattr(browser, "open_context", fake_open)
    sent = []
    monkeypatch.setattr(cli.Notifier, "send", lambda self, title, *a, **k: sent.append(title) or True)

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import playwright.sync_api as sapi
    monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_watch(types.SimpleNamespace(config=str(cfgp)))
    assert seen["mode"] == "review" and "Auto-buy not armed" in sent
