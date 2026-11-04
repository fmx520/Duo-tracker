"""A closed Chrome window should produce a plain-English message, not a traceback."""
import types

import pytest

pytest.importorskip("playwright")

from duo_tracker import browser, checkout, cli  # noqa: E402
from duo_tracker.logsetup import setup  # noqa: E402


class TargetClosedError(Exception):
    """Same name as Playwright's, which is what browser_closed() keys on."""


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(checkout, "STATE_DIR", tmp_path / "state")
    yield tmp_path / "state"
    setup(None)


def test_closed_window_is_explained_not_dumped(state, tmp_path, monkeypatch, capsys):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [512gb-nightsky]\n")
    err = TargetClosedError("Page.goto: Target page, context or browser has been closed")
    monkeypatch.setattr(cli, "cmd_rehearse", lambda args: (_ for _ in ()).throw(err))
    assert cli.main(["-c", str(cfgp), "rehearse"]) == 1
    out = capsys.readouterr().out
    assert "Chrome window closed before `rehearse` finished" in out and "Traceback" not in out
    assert "Traceback" in (state / "logs" / "duo-tracker.log").read_text()  # details kept for debugging


def test_config_error_shows_the_actual_message(state, tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "check"]) == cli.EXIT_NO_RESTART
    out = capsys.readouterr().out
    assert "missing.yaml" in out and "error: e\n" not in out


def test_rehearse_stops_cleanly_when_window_closed_during_bag_cleanup(state, tmp_path, monkeypatch):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [512gb-nightsky]\n")
    page = types.SimpleNamespace(closed=False, is_closed=lambda: page.closed)

    def empty_bag(p):
        p.closed = True   # user closes the window while the bag is being emptied
        return 0

    loaded = []
    monkeypatch.setattr(browser, "open_context", lambda pw, cfg: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(browser, "_page", lambda ctx: page)
    monkeypatch.setattr(browser, "save_cookies", lambda ctx, cfg: 1)
    monkeypatch.setattr(browser, "load_buy_page", lambda p: loaded.append(p))
    monkeypatch.setattr(checkout, "signed_in", lambda p: True)
    monkeypatch.setattr(checkout, "empty_bag", empty_bag)

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import playwright.sync_api as sapi
    monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())

    rc = cli.cmd_rehearse(types.SimpleNamespace(config=str(cfgp), pickup_item_url=None))
    assert rc == 2 and loaded == []           # never tried to drive a dead window
    ok, why = checkout.rehearsal_ok()
    assert not ok and "failed" in why


def _rehearse_env(monkeypatch, tmp_path, signed_seq, tty=True):
    """Fake browser for cmd_rehearse up to the sign-in step."""
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("models: [512gb-nightsky]\n")
    page = types.SimpleNamespace(is_closed=lambda: False, bring_to_front=lambda: None)
    seq = iter(signed_seq)
    reached = []
    monkeypatch.setattr(browser, "open_context", lambda pw, cfg: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(browser, "_page", lambda ctx: page)
    monkeypatch.setattr(browser, "save_cookies", lambda ctx, cfg: 1)
    monkeypatch.setattr(browser, "load_buy_page", lambda p: (_ for _ in ()).throw(SystemExit("reached buy page")))
    monkeypatch.setattr(checkout, "signed_in", lambda p, settle_seconds=25: next(seq))
    monkeypatch.setattr(checkout, "empty_bag", lambda p: reached.append("empty_bag") or 0)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty, raising=False)

    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import playwright.sync_api as sapi
    monkeypatch.setattr(sapi, "sync_playwright", lambda: PW())
    return cfgp, reached


@pytest.mark.parametrize("signed", [True, False])
def test_rehearse_never_prompts_and_carries_on(state, tmp_path, monkeypatch, signed):
    """The account page's re-confirm prompt mustn't stop rehearse or ask the
    person anything; checkout itself reveals whether Apple wants a password."""
    cfgp, _ = _rehearse_env(monkeypatch, tmp_path, [signed])
    monkeypatch.setattr("builtins.input", lambda msg="": pytest.fail("rehearse must not prompt"))
    with pytest.raises(SystemExit, match="reached buy page"):
        cli.cmd_rehearse(types.SimpleNamespace(config=str(cfgp), pickup_item_url=None))
