"""Verify session-cookie snapshot/restore keeps the login across restarts."""
import json
import os
import pytest

pw_api = pytest.importorskip("playwright.sync_api")
from duo_tracker import browser  # noqa: E402


@pytest.fixture
def browser_inst():
    if os.path.isdir("/opt/pw-browsers"):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


class FakeCfg:
    def __init__(self, profile_dir):
        self.profile_dir = profile_dir


def _mk_ctx_with_session_cookie(browser_inst):
    ctx = browser_inst.new_context()
    # a session cookie (no expires) + a persistent cookie
    ctx.add_cookies([
        {"name": "aa", "value": "sess", "domain": ".idmsa.apple.com", "path": "/"},
        {"name": "s_fid", "value": "persist", "domain": ".apple.com", "path": "/",
         "expires": 1893456000},
    ])
    return ctx


def test_save_makes_session_cookie_persistent(browser_inst, tmp_path):
    cfg = FakeCfg(tmp_path / "prof")
    cfg.profile_dir.mkdir()
    ctx = _mk_ctx_with_session_cookie(browser_inst)
    browser.save_cookies(ctx, cfg)
    snap = json.loads((tmp_path / "cookies.json").read_text())
    byname = {c["name"]: c for c in snap}
    # session cookie got a real far-future expiry
    assert 1700000000 < byname["aa"]["expires"] < 2100000000
    # persistent cookie kept a real future expiry
    assert byname["s_fid"]["expires"] > 1700000000
    ctx.close()


def test_restore_injects_snapshot_and_preserves_existing(browser_inst, tmp_path):
    cfg = FakeCfg(tmp_path / "prof")
    cfg.profile_dir.mkdir()
    (tmp_path / "cookies.json").write_text(json.dumps([
        {"name": "aa", "value": "sess", "domain": ".idmsa.apple.com", "path": "/", "expires": 2000000000},
        {"name": "s_fid", "value": "stale", "domain": ".apple.com", "path": "/", "expires": 2000000000},
    ]))
    ctx = browser_inst.new_context()
    # existing fresh value for s_fid must win over the stale snapshot
    ctx.add_cookies([{"name": "s_fid", "value": "fresh", "domain": ".apple.com", "path": "/"}])
    browser._restore_cookies(ctx, cfg)
    cookies = {c["name"]: c["value"] for c in ctx.cookies("https://www.apple.com")}
    idmsa = {c["name"]: c["value"] for c in ctx.cookies("https://idmsa.apple.com")}
    assert idmsa.get("aa") == "sess"           # injected
    assert cookies.get("s_fid") == "fresh"      # not clobbered
    ctx.close()


def test_setup_snapshots_cookies_while_window_is_open(monkeypatch, tmp_path):
    """cmd_setup must snapshot cookies on a poll loop — a 'close' event fires
    after the browser is already unreachable, so saving there captures nothing."""
    from types import SimpleNamespace

    from playwright.sync_api import TimeoutError as PWTimeoutError

    from duo_tracker import browser, cli, config

    cfg = SimpleNamespace(profile_dir=tmp_path / "prof", zip="11754")
    cfg.profile_dir.mkdir()
    calls = {"save": 0, "waits": 0}

    class FakePage:
        def goto(self, url):
            pass

    class FakeCtx:
        def wait_for_event(self, event, timeout=0):
            calls["waits"] += 1
            if calls["waits"] < 3:
                raise PWTimeoutError("still open")
            return None  # window closed

    class FakePW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePW())
    monkeypatch.setattr(config, "load", lambda path: cfg)
    monkeypatch.setattr(browser, "open_context", lambda pw, c: FakeCtx())
    monkeypatch.setattr(browser, "_page", lambda ctx: FakePage())

    def fake_save(ctx, c):
        calls["save"] += 1
        return 5

    monkeypatch.setattr(browser, "save_cookies", fake_save)

    args = SimpleNamespace(config=str(tmp_path / "config.yaml"))
    assert cli.cmd_setup(args) == 0
    assert calls["save"] == 2  # one snapshot per poll before the window closed
