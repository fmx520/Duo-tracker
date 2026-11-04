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


OFFSCREEN = """<!doctype html><html><body style="margin:0">
<div style="width:300px; overflow:hidden; white-space:nowrap">
  <div style="display:inline-block; width:2000px"></div>
  <span style="display:inline-block; pointer-events:none">
    <input id="r2" type="radio" name="applecare-options" data-autom="noapplecare" aria-labelledby="r2_label"
           style="opacity:0; width:0; height:0">
    <span id="r2_label">No AppleCare coverage</span>
  </span>
</div>
<script>document.getElementById('r2').addEventListener('click', () => window.picked = true)</script>
</body></html>"""


def test_offscreen_radio_and_label_selected_by_dom_click():
    """Apple's AppleCare row: radio AND label off-screen/unclickable -> 'couldn't select'."""
    from duo_tracker.browser import _check
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(OFFSCREEN)
        _check(page, "noapplecare")
        assert page.locator('[data-autom="noapplecare"]').is_checked()
        assert page.evaluate("window.picked === true")   # page's own click handler ran
        b.close()


RERENDER = """<!doctype html><html><body>
<input type=radio name=dimensionColor data-autom=dimensionColornightsky>
<input type=radio name=dimensionCapacity data-autom=dimensionCapacity512gb>
<input type=radio name=tradeupinline data-autom=choose-noTradeIn>
<input type=radio name=purchase_option_group data-autom=purchaseGroupOptionfullprice id=fp>
<div id=carriers><input type=radio name=carrierModel data-autom="carrierModelUNLOCKED/US" disabled></div>
<div id=ac><input type=radio name=applecare-options data-autom=noapplecare disabled></div>
<script>
  // Like Apple: picking the payment option re-renders the carrier list ~0.7s later
  // (enabled, fresh, unchecked); AppleCare unlocks only once a carrier is picked.
  document.getElementById('fp').addEventListener('change', () => setTimeout(() => {
    document.getElementById('carriers').innerHTML =
      '<input type=radio name=carrierModel data-autom="carrierModelUNLOCKED/US">';
  }, 700));
  document.addEventListener('change', e => {
    if (e.target.name === 'carrierModel') document.querySelector('[data-autom=noapplecare]').disabled = false;
  });
</script></body></html>"""


def test_select_config_waits_for_apples_rerendered_carrier_list():
    """Live Mac run: carrier clicked before Apple enabled/re-rendered it, so no carrier
    stuck, AppleCare stayed locked, and 'noapplecare' couldn't be selected."""
    from duo_tracker.browser import select_config
    from duo_tracker.catalog import resolve
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.set_content(RERENDER)
        select_config(page, resolve("512gb-nightsky"), "UNLOCKED/US")
        for a in ("carrierModelUNLOCKED/US", "noapplecare", "purchaseGroupOptionfullprice"):
            assert page.locator(f'[data-autom="{a}"]').is_checked(), a
        b.close()
