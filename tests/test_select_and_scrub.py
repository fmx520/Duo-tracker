import os

import pytest

from duo_tracker.report import scrub


def test_scrub_hides_trigger_secret_and_tokens():
    cfg = ('trigger:\n  topic: "duo-abc"\n'
           '  secret: "c9f5b917a7dd5401474024f5f58fd78b1f52aa1e99602422662abb3b42dd437b"\n'
           '  token: tk_live_123456\n  healthcheck_url: ""\n  api_key = abcdef123\n')
    out = scrub(cfg)
    assert "c9f5b917" not in out and "tk_live_123456" not in out and "abcdef123" not in out
    assert 'secret: "<secret>"' in out and 'healthcheck_url: ""' in out   # empty values left alone


pw_api = pytest.importorskip("playwright.sync_api")

PAGE = """<!doctype html><html><body style="margin:0">
<div style="height:3000px"></div>
<div style="position:relative">
  <input id="r1" type="radio" name="applecare-options" data-autom="noapplecare" aria-labelledby="r1_label"
         style="position:absolute; left:-9999px">
  <span id="r1_label" onclick="document.getElementById('r1').checked = true">No AppleCare coverage</span>
</div></body></html>"""


def test_hidden_radio_selected_via_its_label():
    """Apple's 'noapplecare' radio is visually hidden; check() said 'outside of the viewport'."""
    from duo_tracker.browser import _check
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(PAGE)
        _check(page, "noapplecare")
        assert page.locator('[data-autom="noapplecare"]').is_checked()
        b.close()
