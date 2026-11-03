"""Trigger mode (TRIGGER_MODE.md): acceptance criteria 1-7. No network."""
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from duo_tracker import trigger as trig
from duo_tracker.config import load

SECRET = "s" * 64
PART = "MK1H4LL/A"           # 512gb-nightsky
WW = "R068"                  # Walt Whitman, inside the store filter


def cfg_text(mode="trigger", topic="duo-trigger-abc", secret=SECRET, extra=""):
    return (
        "models: [512gb-nightsky]\n"
        'stores: ["Walt Whitman", "R068", "Manhasset", "R327"]\n'
        "ntfy: {topic: alerts-topic}\n"
        f"checkout: {{mode: {mode}}}\n"
        "health: {daily_summary_hour: null, healthcheck_url: 'https://hc.example/abc'}\n"
        f'trigger: {{topic: "{topic}", secret: "{secret}", action: submit}}\n' + extra
    )


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text())
    return load(p)


@pytest.fixture
def nonces(tmp_path):
    return trig.NonceStore(tmp_path / "nonces.json")


def now():
    return datetime.now(timezone.utc)


def check(raw, cfg, nonces, **kw):
    return trig.validate(raw if isinstance(raw, str) else json.dumps(raw), secret=cfg.trigger_secret,
                         parts={m.part for m in cfg.models}, store_allowed=cfg.store_allowed,
                         max_age_seconds=cfg.trigger_max_age_seconds, nonces=nonces, **kw)


# ── 1. unsigned / wrong sig / expired / future / replayed ──────────────────────
def test_valid_trigger_passes(cfg, nonces):
    t = check(trig.make(PART, WW, SECRET), cfg, nonces)
    assert (t.part, t.store_number) == (PART, WW)


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p.pop("sig"), "unsigned"),
    (lambda p: p.update(sig="0" * 64), "bad signature"),
    (lambda p: p.update(store_number="R327"), "bad signature"),          # tampered after signing
    (lambda p: p.update(sig=trig.sign(p, "x" * 64)), "bad signature"),   # signed with another key
])
def test_bad_signatures_rejected(cfg, nonces, mutate, reason):
    p = trig.make(PART, WW, SECRET)
    mutate(p)
    with pytest.raises(trig.TriggerRejected, match=reason):
        check(p, cfg, nonces)


def test_expired_and_future_rejected(cfg, nonces):
    old = trig.make(PART, WW, SECRET, seen_at=now() - timedelta(seconds=91))
    with pytest.raises(trig.TriggerRejected, match="expired"):
        check(old, cfg, nonces)
    future = trig.make(PART, WW, SECRET, seen_at=now() + timedelta(seconds=60))
    with pytest.raises(trig.TriggerRejected, match="future"):
        check(future, cfg, nonces)


def test_replayed_nonce_rejected_even_after_restart(cfg, tmp_path):
    p = trig.make(PART, WW, SECRET)
    check(p, cfg, trig.NonceStore(tmp_path / "n.json"))
    with pytest.raises(trig.TriggerRejected, match="replayed"):
        check(p, cfg, trig.NonceStore(tmp_path / "n.json"))   # fresh store object = after a restart


def test_garbage_and_wrong_version_rejected(cfg, nonces):
    with pytest.raises(trig.TriggerRejected, match="not JSON"):
        check("buy now!!", cfg, nonces)
    p = trig.make(PART, WW, SECRET)
    p["v"] = 2
    p["sig"] = trig.sign(p, SECRET)
    with pytest.raises(trig.TriggerRejected, match="version"):
        check(p, cfg, nonces)


# ── 2. unknown part / unfiltered store ────────────────────────────────────────
def test_unknown_part_and_unfiltered_store_rejected(cfg, nonces):
    with pytest.raises(trig.TriggerRejected, match="not a watched model"):
        check(trig.make("MK1F4LL/A", WW, SECRET), cfg, nonces)    # 256GB: real part, not watched
    with pytest.raises(trig.TriggerRejected, match="outside the configured store filter"):
        check(trig.make(PART, "R139", SECRET), cfg, nonces)        # Smith Haven: not in this filter


# ── 6. config validation ──────────────────────────────────────────────────────
@pytest.mark.parametrize("kw,msg", [
    (dict(secret=""), "needs trigger.secret"),
    (dict(topic=""), "needs trigger.topic"),
    (dict(topic="alerts-topic"), "must differ from ntfy.topic"),
    (dict(secret="short"), "too short"),
])
def test_config_refuses_unsafe_trigger_setup(tmp_path, kw, msg):
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text(**kw))
    with pytest.raises(ValueError, match=msg):
        load(p)


def test_other_modes_dont_need_trigger_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text(mode="submit", secret="", topic=""))
    assert load(p).checkout_mode == "submit"


# ── harness for the listener loop ─────────────────────────────────────────────
pytest.importorskip("playwright")
from duo_tracker import browser, checkout, cli, health  # noqa: E402
from duo_tracker.availability import Availability, StoreStock  # noqa: E402


def stock(*numbers):
    names = {"R068": "Walt Whitman", "R327": "Manhasset", "R139": "Smith Haven"}
    return Availability(part=PART, has_pickup_data=True, stores=[
        StoreStock(names[n], n, "", "9 mi", 9.0, True, "Today") for n in numbers])


class Harness:
    def __init__(self, tmp_path, monkeypatch, apple=None):
        self.state = tmp_path / "state"
        monkeypatch.setattr(checkout, "STATE_DIR", self.state)
        self.cfgp = tmp_path / "c.yaml"
        self.cfgp.write_text(cfg_text())
        self.cfg = load(self.cfgp)
        self.apple = list(apple or [])    # one Availability (or Exception) per confirming check
        self.acted, self.sent, self.pings, self.checks = [], [], [], 0

        def fake_round(page, models, carrier, **kw):
            self.checks += 1
            yield models[0], self.apple.pop(0)

        def fake_act(page, cfg, notifier, model, stores):
            self.acted.append((cfg.checkout_mode, model.part, [s.store_number for s in stores]))
            return {"outcome": "ordered", "store_number": stores[0].store_number, "detail": "", "checkout_s": 9}

        class PW:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        import playwright.sync_api as sapi
        monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())
        monkeypatch.setattr(browser, "open_context", lambda pw, cfg: types.SimpleNamespace(pages=[object()], close=lambda: None))
        monkeypatch.setattr(browser, "check_round", fake_round)
        monkeypatch.setattr(checkout, "empty_bag", lambda page: 0)
        monkeypatch.setattr(checkout, "signed_in", lambda page: True)
        monkeypatch.setattr(cli, "_act_on_hit", fake_act)
        monkeypatch.setattr(cli, "_hold_after_acting", lambda h: None)
        monkeypatch.setattr(cli.Notifier, "send", lambda s, title, *a, **k: self.sent.append(title) or True)
        monkeypatch.setattr(health.requests, "get", lambda url, timeout=5: self.pings.append(url))

    def run(self, *messages, events=None):
        evs = events or ([trig.Event("connected")] + [trig.Event("message", m if isinstance(m, str) else json.dumps(m))
                                                       for m in messages])
        return cli._watch_triggers(self.cfg, listen_fn=lambda *a, **k: iter(evs))


# ── 1+2 end to end: rejected triggers never reach Apple or checkout ───────────
def test_rejected_triggers_buy_nothing_and_alert(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    bad = trig.make(PART, WW, SECRET)
    bad["sig"] = "0" * 64
    expired = trig.make(PART, WW, SECRET, seen_at=now() - timedelta(minutes=5))
    unfiltered = trig.make(PART, "R139", SECRET)
    assert h.run("hello", bad, expired, unfiltered) == 0
    assert h.acted == [] and h.checks == 0
    assert h.sent.count("⚠️ Trigger rejected") == 1    # alerted, but flood-guarded


# ── 3. valid trigger, Apple disagrees → no buy, alert ─────────────────────────
def test_trigger_not_matching_stock_does_not_buy(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[stock("R327")])   # Manhasset has it, Walt Whitman doesn't
    h.run(trig.make(PART, WW, SECRET))
    assert h.checks == 1 and h.acted == []
    assert "Trigger did not match stock" in h.sent


def test_trigger_confirm_blocked_does_not_buy(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[browser.Blocked("HTTP 541")])
    h.run(trig.make(PART, WW, SECRET))
    assert h.acted == [] and any("couldn't confirm" in t for t in h.sent)


# ── 4. confirmed trigger buys once; duplicate nonce after it buys nothing ────
def test_confirmed_trigger_buys_once_and_replay_is_rejected(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[stock("R068", "R327")])
    p = trig.make(PART, WW, SECRET)
    h.run(p)
    assert h.acted == [("review", PART, ["R068", "R327"])]   # no rehearsal on file → review, not submit
    recs = [json.loads(x) for x in (h.state / "drops.jsonl").read_text().splitlines()]
    assert [r["type"] for r in recs] == ["start", "outcome"] and recs[0]["window"] == "trigger"

    h2 = Harness(tmp_path, monkeypatch, apple=[stock("R068")])
    h2.run(p)                                               # same payload again, new process
    assert h2.acted == [] and h2.checks == 0


def test_submit_used_when_rehearsal_passed(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[stock("R068")])
    monkeypatch.setattr(checkout, "rehearsal_ok", lambda: (True, "rehearsal passed"))
    h.run(trig.make(PART, WW, SECRET))
    assert h.acted[0][0] == "submit"


# ── 5. order.lock still blocks ────────────────────────────────────────────────
def test_order_lock_blocks_trigger_mode(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[stock("R068")])
    h.state.mkdir(parents=True)
    checkout.lock_path().write_text(json.dumps({"model": "x", "store": "y", "at": "z"}))
    assert h.run(trig.make(PART, WW, SECRET)) == cli.EXIT_NO_RESTART
    assert h.acted == [] and h.checks == 0


# ── 7. reconnects; dead-man switch pinged while connected ─────────────────────
class FakeResp:
    def __init__(self, lines, status=200, then_raise=None):
        self.lines, self.status_code, self.then_raise = lines, status, then_raise

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass

    def iter_lines(self, decode_unicode=True):
        yield from (json.dumps(x) for x in self.lines)
        if self.then_raise:
            raise self.then_raise


def test_listener_reconnects_with_backoff_and_resumes_since_last_id():
    calls, sleeps = [], []
    conns = iter([
        FakeResp([{"event": "open"}, {"event": "message", "id": "m1", "message": "a"}],
                 then_raise=ConnectionError("reset")),
        FakeResp([], then_raise=TimeoutError("read timeout")),
        FakeResp([{"event": "open"}, {"event": "keepalive"}, {"event": "message", "id": "m2", "message": "b"}]),
    ])

    def http_get(url, **kw):
        calls.append(kw["params"])
        return next(conns)

    gen = trig.listen("https://ntfy.sh", "t", backoff_s=5, http_get=http_get, sleep=sleeps.append)
    kinds = [next(gen) for _ in range(8)]
    assert [e.kind for e in kinds] == ["connected", "message", "disconnected", "disconnected",
                                       "connected", "keepalive", "message", "disconnected"]
    assert calls[0] == {} and calls[1] == {"since": "m1"} and calls[2] == {"since": "m1"}
    assert sleeps == [5, 10]              # doubling while it can't reconnect
    next(gen)                             # resume past the 3rd disconnect → its sleep runs
    assert sleeps[2] == 5                 # reset after a successful open


def test_listener_stops_on_auth_failure():
    gen = trig.listen("https://ntfy.sh", "t", token="bad", http_get=lambda url, **kw: FakeResp([], status=403),
                      sleep=lambda s: None)
    with pytest.raises(PermissionError):
        next(gen)


def test_dead_man_switch_pinged_while_connected_and_stall_when_down(tmp_path, monkeypatch):
    sent, pings, clock = [], [], {"t": 1000.0}
    hl = health.Health(types.SimpleNamespace(send=lambda title, *a, **k: sent.append(title)), daily_hour=None,
                       stall_minutes=10, healthcheck_url="https://hc.example/abc", clock=lambda: clock["t"],
                       http_get=pings.append, kind="trigger")
    for _ in range(3):                    # keepalives every ~45s → pings, throttled to 1/min
        hl.alive()
        clock["t"] += 45
        hl.tick()
    assert pings == ["https://hc.example/abc", "https://hc.example/abc"]
    clock["t"] += 11 * 60                 # disconnected: no keepalives for 11 min
    hl.tick()
    assert sent == ["⚠️ Duo-tracker stalled"] and pings[-1].endswith("/fail")
    hl.alive()
    assert sent[-1] == "Duo-tracker recovered"


def test_watch_loop_feeds_health_on_keepalive(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.run(events=[trig.Event("connected"), trig.Event("disconnected", "reset"), trig.Event("connected")])
    assert h.pings and all(u == "https://hc.example/abc" for u in h.pings)


def test_trigger_init_writes_private_section_and_rotates(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models: [512gb-nightsky]\nntfy: {topic: alerts-topic}\n")
    args = types.SimpleNamespace(config=str(p), rotate=False)
    assert cli.cmd_trigger_init(args) == 0
    first = load(p)
    assert first.trigger_topic.startswith("duo-trigger-") and len(first.trigger_secret) == 64
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert cli.cmd_trigger_init(args) == 1                       # won't silently overwrite
    assert cli.cmd_trigger_init(types.SimpleNamespace(config=str(p), rotate=True)) == 0
    second = load(p)
    assert second.trigger_secret != first.trigger_secret and second.trigger_topic != first.trigger_topic


# ── cross-language signing vector (kelindi detector signs in JS) ──────────────
VECTOR = {"v": 1, "part": "MK1H4LL/A", "store_number": "R327", "seen_at": "2026-11-03T12:40:48.247Z",
          "nonce": "00112233445566778899aabbccddeeff"}
VECTOR_SIG = "8d588eefa7a2ca77114f1e81be838591d473f7a89121df1b3ae3e6f8099b54d9"   # same constant in apple-stock test


def test_signing_vector_matches_js_detector():
    assert trig.sign(VECTOR, "k" * 64) == VECTOR_SIG
    assert trig.canonical(VECTOR) == (b'{"nonce":"00112233445566778899aabbccddeeff","part":"MK1H4LL/A",'
                                      b'"seen_at":"2026-11-03T12:40:48.247Z","store_number":"R327","v":1}')


def test_js_style_utc_z_timestamps_validate(cfg, nonces):
    seen = now().strftime("%Y-%m-%dT%H:%M:%S.") + "123Z"      # what Date.toISOString() emits
    p = {"v": 1, "part": PART, "store_number": WW, "seen_at": seen, "nonce": "abc123"}
    p["sig"] = trig.sign(p, SECRET)
    assert check(p, cfg, nonces).store_number == WW


# ── radar source ──────────────────────────────────────────────────────────────
class RadarResp:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


def radar_state(stock, ok_age_s=2):
    ok = (now() - timedelta(seconds=ok_age_s)).isoformat().replace("+00:00", "Z")
    return {"stock": stock, "events": [], "status": {"last_ok_at": ok, "stores_seen": 29}}


def iso_ago(s):
    return (now() - timedelta(seconds=s)).isoformat().replace("+00:00", "Z")


def run_radar(responses, n, **kw):
    sleeps, it = [], iter(responses)

    def http_get(url, **k):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return RadarResp(r)
    gen = trig.radar("https://radar.example", secret=SECRET, parts={PART}, store_allowed=lambda s: s in {"R068", "R327"},
                     poll_s=5, http_get=http_get, sleep=sleeps.append, **kw)
    return [next(gen) for _ in range(n)], sleeps


def test_radar_emits_signed_trigger_for_new_watched_hit(cfg, nonces):
    hit = {"store_id": "R327", "part_number": PART, "changed_at": iso_ago(3)}
    other_part = {"store_id": "R327", "part_number": "MK1F4LL/A", "changed_at": iso_ago(3)}
    other_store = {"store_id": "R095", "part_number": PART, "changed_at": iso_ago(3)}
    evs, _ = run_radar([radar_state([hit, other_part, other_store]), radar_state([hit])], 3)
    assert [e.kind for e in evs] == ["keepalive", "message", "keepalive"]   # 2nd poll: same hit, not re-sent
    t = check(evs[1].text, cfg, nonces)
    assert (t.part, t.store_number) == (PART, "R327") and 2 <= t.age() <= 10   # age measured from radar's changed_at


def test_radar_stale_or_down_reports_disconnected_and_backs_off():
    evs, sleeps = run_radar([radar_state([], ok_age_s=300), ConnectionError("dns"), radar_state([])], 3)
    assert [e.kind for e in evs] == ["disconnected", "disconnected", "keepalive"]
    assert "stale" in evs[0].text and "unreachable" in evs[1].text
    assert 8.5 <= sleeps[1] <= 11.5        # doubled (±15% jitter) after a failure


def test_radar_old_hit_is_rejected_as_expired(cfg, nonces):
    evs, _ = run_radar([radar_state([{"store_id": "R068", "part_number": PART, "changed_at": iso_ago(400)}])], 2)
    with pytest.raises(trig.TriggerRejected, match="expired"):
        check(evs[1].text, cfg, nonces)


def test_merged_reports_fatal_and_ends():
    def good():
        yield trig.Event("keepalive")

    def bad():
        raise PermissionError("HTTP 403")
        yield  # pragma: no cover

    out = list(trig.merged({"a": good, "b": bad}))
    kinds = {(s, e.kind) for s, e in out}
    assert ("a", "keepalive") in kinds and ("a", "ended") in kinds and ("b", "fatal") in kinds


def test_radar_only_config_needs_no_topic(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text(topic="").replace("action: submit}", "action: submit, sources: [radar]}"))
    c = load(p)
    assert c.trigger_sources == ("radar",)
    p.write_text(cfg_text().replace("action: submit}", "action: submit, sources: [radar], radar_poll_seconds: 1}"))
    with pytest.raises(ValueError, match="radar_poll_seconds"):
        load(p)
    p.write_text(cfg_text().replace("action: submit}", "action: submit, sources: [carrier-pigeon]}"))
    with pytest.raises(ValueError, match="trigger.sources"):
        load(p)


def test_radar_hit_confirmed_by_apple_runs_checkout(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, apple=[stock("R327")])
    h.cfg.trigger_sources = ("ntfy", "radar")
    hit = trig.make(PART, "R327", SECRET, seen_at=now() - timedelta(seconds=4))

    def radar_fn(*a, **k):
        yield trig.Event("keepalive")
        yield trig.Event("message", json.dumps(hit))

    cli._watch_triggers(h.cfg, listen_fn=lambda *a, **k: iter([trig.Event("connected")]), radar_fn=radar_fn)
    assert h.acted == [("review", PART, ["R327"])]


def test_fatal_source_alone_exits_no_restart(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)

    def listen(*a, **k):
        raise PermissionError("ntfy refused the subscription (HTTP 403)")
        yield  # pragma: no cover
    assert cli._watch_triggers(h.cfg, listen_fn=listen) == cli.EXIT_NO_RESTART
    assert any("stopped" in t for t in h.sent)


def test_trigger_env_writes_detector_settings_privately(tmp_path):
    det = tmp_path / "apple-stock"
    det.mkdir()
    (det / ".env.example").write_text("POSTAL_CODE=M5V3L9\nCOUNTRY=ca\nNTFY_TOPIC=\nDUO_TRIGGER_TOPIC=\nDUO_TRIGGER_SECRET=\n")
    p = tmp_path / "c.yaml"
    p.write_text(cfg_text() + 'zip: "11754"\nmax_miles: 30\n')
    assert cli.cmd_trigger_env(types.SimpleNamespace(config=str(p), detector_dir=str(det))) == 0
    env = dict(line.split("=", 1) for line in (det / ".env").read_text().splitlines() if "=" in line)
    assert env["COUNTRY"] == "us" and env["POSTAL_CODE"] == "11754" and env["MODELS"] == PART
    assert env["MAX_DISTANCE_KM"] == "48" and env["DUO_TRIGGER_SECRET"] == SECRET
    assert env["DUO_TRIGGER_TOPIC"] == "duo-trigger-abc" and env["NTFY_TOPIC"] == ""
    assert oct((det / ".env").stat().st_mode)[-3:] == "600"
