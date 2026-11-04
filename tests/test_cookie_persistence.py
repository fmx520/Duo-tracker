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
