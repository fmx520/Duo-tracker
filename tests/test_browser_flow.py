"""Exercise the real browser code against a fake apple.com served via request routing.

Nothing leaves the machine: every apple.com request is answered by `route()`.
The fake page mirrors the buy page's real hooks (data-autom radio inputs, an
"add-to-cart" button, and a fetch to /shop/fulfillment-messages once a full
configuration is chosen), as observed on the live page in Nov 2026.
"""
import json
import os

import pytest

pw_api = pytest.importorskip("playwright.sync_api")

from duo_tracker.availability import matching_stores  # noqa: E402
from duo_tracker.browser import Blocked, check_model, go_to_bag  # noqa: E402
from duo_tracker.catalog import resolve  # noqa: E402

BUY_PAGE = """<!doctype html><html><body>
<input type=radio name=dimensionColor value=starwhite data-autom=dimensionColorstarwhite>
<input type=radio name=dimensionColor value=nightsky data-autom=dimensionColornightsky>
<input type=radio name=dimensionCapacity value=256gb data-autom=dimensionCapacity256gb>
<input type=radio name=dimensionCapacity value=1tb data-autom=dimensionCapacity1tb>
<input type=radio name=tradeupinline value=noTradeIn data-autom=choose-noTradeIn>
<input type=radio name=purchase_option_group value=fullprice data-autom=purchaseGroupOptionfullprice>
<input type=radio name=carrierModel value="UNLOCKED/US" data-autom="carrierModelUNLOCKED/US">
<input type=radio name=applecare-options value=on data-autom=noapplecare>
<button name=add-to-cart data-autom=add-to-cart onclick="location.href='/shop/bag'">Add to Bag</button>
<script>
const parts = {"256gb|nightsky":"MK1F4LL/A","256gb|starwhite":"MK1E4LL/A","1tb|starwhite":"MK1J4LL/A","1tb|nightsky":"MK1L4LL/A"};
document.addEventListener('change', () => {
  const v = n => (document.querySelector(`input[name="${n}"]:checked`)||{}).value;
  if (!v('dimensionColor') || !v('dimensionCapacity') || !v('carrierModel')) return;
  const p = parts[v('dimensionCapacity')+'|'+v('dimensionColor')];
  const key = p + v('carrierModel');
  if (window.lastKey === key) return;
  window.lastKey = key;
  fetch('/shop/fulfillment-messages?fae=true&parts.0=' + p + '&cppart=' + v('carrierModel'));
});
</script></body></html>"""

BAG_PAGE = """<!doctype html><html><body><h1>Bag</h1>
<button data-autom=checkout onclick="location.href='/shop/checkout'">Check Out</button></body></html>"""


def avail_json(part, display):
    return json.dumps({"body": {"content": {"pickupMessage": {"stores": [
        {"storeName": "Fifth Avenue", "storeNumber": "R095", "city": "New York",
         "partsAvailability": {part: {"pickupDisplay": display, "pickupSearchQuote": "Available Today"}}}]}}}})


@pytest.fixture
def page_factory():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        browser = pw.chromium.launch()

        def make(status=200, display="available"):
            ctx = browser.new_context()
            seen = []

            def handle(route):
                url = route.request.url
                seen.append(url)
                if "/shop/fulfillment-messages" in url:
                    from urllib.parse import parse_qs, urlparse
                    part = parse_qs(urlparse(url).query)["parts.0"][0]
                    if status != 200:
                        return route.fulfill(status=status, body="")
                    return route.fulfill(status=200, content_type="application/json", body=avail_json(part, display))
                if url.endswith("/shop/bag"):
                    return route.fulfill(status=200, content_type="text/html", body=BAG_PAGE)
                if url.endswith("/shop/checkout"):
                    return route.fulfill(status=200, content_type="text/html", body="<h1>Checkout</h1>")
                return route.fulfill(status=200, content_type="text/html", body=BUY_PAGE)

            ctx.route("**/*", handle)
            page = ctx.new_page()
            page.seen = seen
            return page

        yield make
        browser.close()


def test_in_stock_then_bag(page_factory):
    page = page_factory()
    model = resolve("1tb-starwhite")
    av = check_model(page, model, "UNLOCKED/US")
    assert av.part == "MK1J4LL/A"
    assert [s.store_name for s in matching_stores(av, [])] == ["Fifth Avenue"]
    # the page's own request carried the right part and carrier
    req = next(u for u in page.seen if "fulfillment-messages" in u)
    assert "MK1J4LL/A" in req and "UNLOCKED" in req

    assert go_to_bag(page)
    assert page.url.endswith("/shop/bag")


def test_bag_mode_stops_at_bag(page_factory):
    page = page_factory()
    model = resolve("256gb-nightsky")
    check_model(page, model, "UNLOCKED/US")
    assert go_to_bag(page)
    assert page.url.endswith("/shop/bag")


def test_out_of_stock(page_factory):
    page = page_factory(display="unavailable")
    av = check_model(page, resolve("256gb-nightsky"), "UNLOCKED/US")
    assert av.has_pickup_data and av.in_stock_stores == []


@pytest.mark.parametrize("status", [541, 403, 429])
def test_block_is_reported_not_retried(page_factory, status):
    page = page_factory(status=status)
    with pytest.raises(Blocked, match=str(status)):
        check_model(page, resolve("256gb-nightsky"), "UNLOCKED/US")
    assert sum("fulfillment-messages" in u for u in page.seen) == 1


def test_round_loads_page_once_for_many_models(page_factory):
    from duo_tracker.browser import check_round
    page = page_factory()
    models = [resolve(k) for k in ("256gb-nightsky", "256gb-starwhite", "1tb-starwhite")]
    results = list(check_round(page, models, "UNLOCKED/US"))
    assert [m.part for m, _ in results] == ["MK1F4LL/A", "MK1E4LL/A", "MK1J4LL/A"]
    assert all(not isinstance(av, Exception) and av.in_stock_stores for _, av in results)
    page_loads = [u for u in page.seen if "fulfillment" not in u]
    assert len(page_loads) == 1, page_loads
