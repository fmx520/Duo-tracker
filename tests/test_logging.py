"""Log file, crash alerts, drop history, and the scrubbed support report."""
import json
import logging
import types
from datetime import datetime, timedelta

import pytest

pytest.importorskip("playwright")

from duo_tracker import browser, checkout, cli  # noqa: E402
from duo_tracker.drops import DropLog, export_csv, load, summarize  # noqa: E402
from duo_tracker.logsetup import log_path, setup  # noqa: E402
from duo_tracker.report import build, scrub  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(checkout, "STATE_DIR", d)
    yield d
    setup(None)  # detach file handler so later tests don't write into tmp


def test_log_file_gets_details_screen_stays_short(state, capsys):
    setup(state)
    try:
        raise ValueError("selector vanished")
    except ValueError as e:
        cli.say("256GB Night Sky: error: selector vanished", logging.WARNING, exc=e)
    cli.say("all good")
    screen = capsys.readouterr().out
    text = log_path(state).read_text()
    assert "error: selector vanished" in screen and "Traceback" not in screen
    assert "WARNING" in text and "Traceback (most recent call last)" in text and "all good" in text


def test_crash_in_watch_notifies_and_exits_for_restart(state, tmp_path, monkeypatch):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [256gb-nightsky]\nntfy: {topic: abc}\n")
    sent = []
    monkeypatch.setattr(cli.Notifier, "send", lambda self, title, msg, **k: sent.append((title, msg)) or True)
    monkeypatch.setattr(cli, "cmd_watch", lambda args: (_ for _ in ()).throw(TimeoutError("page froze")))
    assert cli.main(["-c", str(cfgp), "watch"]) == 1   # 1 => systemd restarts it
    assert sent[0][0] == "💥 Duo-tracker crashed" and "TimeoutError: page froze" in sent[0][1]
    assert "Traceback" in log_path(state).read_text()


def test_no_screen_is_not_a_crash(state, tmp_path, monkeypatch):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [256gb-nightsky]\n")
    sent = []
    monkeypatch.setattr(cli.Notifier, "send", lambda self, *a, **k: sent.append(a) or True)
    monkeypatch.setattr(cli, "cmd_watch", lambda args: (_ for _ in ()).throw(browser.NoScreenError("no screen")))
    assert cli.main(["-c", str(cfgp), "watch"]) == cli.EXIT_NO_RESTART
    assert sent == []


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 4, 8, 0, 0)

    def __call__(self):
        return self.t


def test_drop_history_roundtrip(tmp_path):
    c = Clock()
    log = DropLog(tmp_path / "drops.jsonl", now=c)
    a = log.start(key="pickup:R068:MK1F4LL/A", model="iPhone Duo 256GB Night Sky", part="MK1F4LL/A",
                  store="Walt Whitman", store_number="R068", kind="pickup", notice_gap_s=27.4, window="peak 07:30–09:30")
    log.outcome(a, "ordered", "$2,176.41", 11.2)
    c.t += timedelta(seconds=64)
    log.end(a)
    b = log.start(key="pickup:R060:MK1H4LL/A", model="iPhone Duo 512GB Night Sky", part="MK1H4LL/A",
                  store="Roosevelt Field", store_number="R060", kind="pickup", notice_gap_s=None, window="off-peak")
    log.outcome(b, "needs you", "review: total over cap")
    (tmp_path / "drops.jsonl").open("a").write("not json\n")   # a torn line must not break reading
    drops = load(tmp_path / "drops.jsonl")
    assert [d["store"] for d in drops] == ["Walt Whitman", "Roosevelt Field"]
    assert drops[0]["seen_s"] == 64 and drops[0]["outcome"] == "ordered" and "seen_s" not in drops[1]
    out = summarize(tmp_path / "drops.jsonl", now=lambda: c.t)
    assert "2 drops seen" in out and "ordered 1" in out and "needs you 1" in out
    assert "noticed ≤27s" in out and "seen 64s" in out
    assert export_csv(tmp_path / "drops.jsonl", tmp_path / "d.csv") == 2
    assert (tmp_path / "d.csv").read_text().splitlines()[0].startswith("first_seen,last_seen,seen_s")


def test_watch_records_drop_start_outcome_and_end(state, tmp_path, monkeypatch):
    from duo_tracker.availability import Availability, StoreStock
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [256gb-nightsky]\ncheckout: {mode: alert}\nhealth: {daily_summary_hour: null}\n")
    ww = lambda avail: StoreStock("Walt Whitman", "R068", "Huntington Station", "9.2 mi", 9.2, avail, "Today")  # noqa: E731
    rounds = iter([False, True, True, False])

    def fake_round(page, models, carrier):
        yield models[0], Availability(part=models[0].part, stores=[ww(next(rounds))], has_pickup_data=True)

    sleeps = []

    def fake_sleep(secs, on_tick=None):
        sleeps.append(secs)
        if len(sleeps) == 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(browser, "open_context", lambda pw, cfg: types.SimpleNamespace(pages=[object()], close=lambda: None))
    monkeypatch.setattr(browser, "check_round", fake_round)
    monkeypatch.setattr(browser, "sleep_with_status", fake_sleep)
    monkeypatch.setattr(cli.Notifier, "send", lambda self, *a, **k: True)

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import playwright.sync_api as sapi
    monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())

    cli.cmd_watch(types.SimpleNamespace(config=str(cfgp)))
    recs = [json.loads(x) for x in (state / "drops.jsonl").read_text().splitlines()]
    assert [r["type"] for r in recs] == ["start", "outcome", "end"]   # one drop, alerted once, then sold out
    assert recs[0]["store"] == "Walt Whitman" and recs[0]["notice_gap_s"] is not None
    assert recs[1]["outcome"] == "alert only"


def test_act_on_hit_survives_unexpected_errors(monkeypatch):
    from duo_tracker.availability import StoreStock

    class Page:
        def bring_to_front(self): pass
    monkeypatch.setattr(browser, "go_to_bag", lambda page: (_ for _ in ()).throw(RuntimeError("target closed")))
    sent = []
    notifier = types.SimpleNamespace(send=lambda title, msg, **k: sent.append(title))
    cfg = types.SimpleNamespace(checkout_mode="submit", cap_for=lambda m: 2300)
    model = types.SimpleNamespace(name="iPhone Duo 256GB Night Sky", part="MK1F4LL/A", price=1999)
    st = StoreStock("Walt Whitman", "R068", "", "9 mi", 9.0, True, "")
    res = cli._act_on_hit(Page(), cfg, notifier, model, [st])
    assert res["outcome"] == "needs you" and "target closed" in res["detail"] and res["store_number"] == "R068"
    assert sent == ["⚠️ Checkout needs you NOW"]


def test_scrub_removes_personal_details():
    raw = ("Ships to: 25 Main Street, Kings Park NY · parker@example.com · (631) 555-0142 · "
           "card 4242 4242 4242 4242 · Order W123456789 · https://ntfy.sh/duo-3f9a1c7b2e · "
           "https://hc-ping.com/1f2e3d4c-aaaa-bbbb-cccc-1234567890ab · R068 Walt Whitman 9.2 mi $2,176.41 11754")
    out = scrub(raw)
    for leaked in ("25 Main Street", "parker@example.com", "555-0142", "4242 4242", "W123456789",
                   "duo-3f9a1c7b2e", "1f2e3d4c-aaaa"):
        assert leaked not in out, leaked
    assert "<card-number>" in out
    assert scrub("4111111111111111 and 4111-1111-1111-1111") == "<card-number> and <card-number>"
    for kept in ("R068 Walt Whitman", "9.2 mi", "$2,176.41", "11754"):
        assert kept in out, kept


def test_support_report_bundles_and_scrubs(state, tmp_path):
    setup(state)
    cli.say("watch started; ntfy topic duo-3f9a1c7b2e")
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text('zip: "11754"\nntfy:\n  topic: duo-3f9a1c7b2e\nhealth:\n  healthcheck_url: "https://hc-ping.com/abcdef123456"\n')
    (state / "reports").mkdir(parents=True)
    (state / "reports" / "20261104-080102-review.json").write_text(json.dumps(
        {"step": "review", "reason": "couldn't read the order total", "controls": [{"text": "Ships to 25 Oak Lane"}]}))
    checkout.record_rehearsal(True, "delivery ok")
    out = build(state, cfgp)
    text = out.read_text()
    assert out.parent == state / "support"
    for section in ("config.yaml", "rehearsal.json", "Apple blocks", "Drops (last 7 days)",
                    "checkout report 20261104-080102-review.json", "log (last"):
        assert section in text, section
    assert "couldn't read the order total" in text and "watch started" in text
    assert "duo-3f9a1c7b2e" not in text and "abcdef123456" not in text and "25 Oak Lane" not in text


def test_scrub_leaves_timestamps_and_filenames_alone():
    for s in ("20261104-080102-review.json", "2026-11-03 13:46:14,213", "id 1234567890123 not luhn"):
        assert scrub(s) == s, s
