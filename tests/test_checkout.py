"""End-to-end checkout against a fake apple.com answered by request routing.

The fake mirrors the shape of Apple's web checkout (bag -> fulfillment choice ->
store + pickup window -> contact -> payment -> review -> thank-you). It proves
the bot's LOGIC and safety stops; it can't prove Apple's real selectors — that's
what `python -m duo_tracker rehearse` is for.
"""
import json
import os
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

pw_api = pytest.importorskip("playwright.sync_api")

from duo_tracker import checkout  # noqa: E402
from duo_tracker.browser import go_to_bag  # noqa: E402
from duo_tracker.catalog import resolve  # noqa: E402
from duo_tracker.checkout import CheckoutStopped, Plan, empty_bag, parse_total, run  # noqa: E402

MODEL = resolve("256gb-nightsky")
PRICES = {MODEL.part: MODEL.price, "ACC1": 19}
NAMES = {MODEL.part: MODEL.name, "ACC1": "USB-C Charge Cable (1 m)"}
STORES = {"R095": "Fifth Avenue", "R001": "SoHo"}

# Every page fires a beacon every 250ms, like Apple's analytics, so the
# network is never idle; the bot must not depend on it going quiet.
PAGE = ("<!doctype html><html><head><title>{title}</title></head><body>{body}"
        "<script>setInterval(() => fetch('/beacon').catch(() => {{}}), 250)</script></body></html>")


class FakeApple:
    def __init__(self, *, cvv=False, signin=False, tax_rate=0.08875, stores=("R095", "R001"), extra_qty=0):
        self.items, self.orders = [], []
        self.cvv, self.signin, self.tax_rate, self.stores, self.extra_qty = cvv, signin, tax_rate, stores, extra_qty
        self.store = None

    def page(self, title, body):
        return PAGE.format(title=title, body=body)

    def handle(self, route):
        u = urlparse(route.request.url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        html = lambda t, b: route.fulfill(status=200, content_type="text/html", body=self.page(t, b))  # noqa: E731

        if path == "/beacon":
            return route.fulfill(status=204, body="")
        if path == "/shop/buy-iphone/iphone-duo":
            return html("Buy iPhone Duo", f"""<button data-autom="add-to-cart"
                onclick="location.href='/shop/bag?added={MODEL.part}'">Add to Bag</button>""")
        if path.startswith("/shop/product/"):
            return html("Cable", """<button data-autom="add-to-cart"
                onclick="location.href='/shop/bag?added=ACC1'">Add to Bag</button>""")
        if path == "/shop/bag":
            if "added" in q:
                self.items += [q["added"]] * (1 + self.extra_qty)
            if "remove" in q and self.items:
                self.items.pop(0)
            rows = "".join(f"<div>{NAMES[p]} <button data-autom='bag-item-remove-button'"
                           f" onclick=\"location.href='/shop/bag?remove=1'\">Remove</button></div>" for p in self.items)
            return html("Bag", f"<h1>Your bag</h1>{rows}<a href='/shop/accessories'>Continue Shopping</a>"
                               "<button data-autom='checkout' onclick=\"location.href='/shop/checkout?_s=Fulfillment'\">Check Out</button>")
        if path == "/signin":
            return html("Sign in", "<input type=email><input type=password><button>Continue</button>")
        if path == "/shop/checkout/thankyou":
            if "placed" in q:
                self.orders.append(list(self.items))
                self.items = []  # Apple empties the bag after an order
            return html("Thank you", "<h1>Thank you for your order.</h1><p>Your order number is W123456789.</p>")
        if path != "/shop/checkout":
            return html("x", "")

        step = q.get("_s")
        if step == "Fulfillment" and self.signin:
            return html("Redirecting", "<script>location.replace('/signin')</script>")
        if step == "Fulfillment":
            radios = "".join(f"<label><input type=radio name=storeSelect value={n}> {STORES[n]}<br>Today</label>"
                             for n in self.stores)
            return html("Checkout", f"""
              <h2>How would you like to get your order?</h2>
              <button data-autom="fulfillment-delivery-button" onclick="del()">I'd like it delivered</button>
              <button data-autom="fulfillment-pickup-button" onclick="pick()">I'll pick it up</button>
              <div id=p style="display:none">{radios}
                <select name=pickupWindow><option value="">Choose</option><option value=w1>Today 4–5 pm</option></select>
              </div>
              <button id=c style="display:none" data-autom="fulfillment-continue-button" onclick="go()">Continue</button>
              <script>
                let mode = '';
                function pick(){{ mode='pickup'; p.style.display='block'; c.style.display='inline'; }}
                function del(){{ mode='delivery'; p.style.display='none'; c.style.display='inline'; }}
                function go(){{
                  const s = document.querySelector('input[name=storeSelect]:checked');
                  const w = document.querySelector('select[name=pickupWindow]').value;
                  if (mode==='pickup' && (!s || !w)) return;
                  location.href = mode==='pickup' ? '/shop/checkout?_s=PickupContact&store='+s.value
                                                  : '/shop/checkout?_s=Shipping';
                }}
              </script>""")
        if step == "PickupContact":
            self.store = q.get("store")
            return html("Checkout", "<h2>Who's picking up?</h2><button data-autom='pickup-contact-continue-button'"
                                    " onclick=\"location.href='/shop/checkout?_s=Billing'\">Continue to Payment</button>")
        if step == "Shipping":
            self.store = None
            return html("Checkout", "<h2>Where should we send it?</h2><button "
                                    "onclick=\"location.href='/shop/checkout?_s=Billing'\">Continue to Payment</button>")
        if step == "Billing":
            cvv = "<label>Security code <input autocomplete=cc-csc name=cvv value=SECRET123></label>" if self.cvv else ""
            return html("Checkout", f"<h2>Payment</h2><p>Visa •••• 4242</p>{cvv}"
                                    "<button onclick=\"location.href='/shop/checkout?_s=Review'\">Continue to Review</button>")
        if step == "Review":
            sub = sum(PRICES[p] for p in self.items)
            tax = round(sub * self.tax_rate, 2)
            where = f"Pickup at Apple {STORES[self.store]}" if self.store else "Ships to: 1 Main St"
            lines = "".join(f"<div>{NAMES[p]}</div>" for p in self.items)
            return html("Review", f"""<h2>Review your order</h2>{lines}<p>{where}</p>
              <p>Subtotal ${sub:,.2f}</p><p>Estimated Tax ${tax:,.2f}</p><p>Total ${sub + tax:,.2f}</p>
              <button data-autom="place-order-button" onclick="location.href='/shop/checkout/thankyou?placed=1'">Place Your Order</button>""")
        return html("x", "")


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(checkout, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


@pytest.fixture
def browser():
    # Only point at the CI browser dir when it actually exists; otherwise leave
    # PLAYWRIGHT_BROWSERS_PATH alone so Playwright uses its own default cache.
    if os.path.isdir("/opt/pw-browsers"):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with pw_api.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


def open_page(browser, fake: FakeApple):
    ctx = browser.new_context()

    ctx.route("**/*", fake.handle)
    page = ctx.new_page()
    page.goto("https://www.apple.com/shop/buy-iphone/iphone-duo")
    assert go_to_bag(page)
    return page


def pickup_plan(**kw):
    base = dict(model=MODEL, fulfillment="pickup", store_number="R095", store_name="Fifth Avenue",
                max_total=round(MODEL.price * 1.11, 2), submit=True)
    base.update(kw)
    return Plan(**base)


def test_full_auto_checkout_places_one_order(browser, state_dir):
    fake = FakeApple()
    page = open_page(browser, fake)
    res = run(page, pickup_plan())
    assert res.placed and res.confirmation == "W123456789"
    assert res.store == "Fifth Avenue" and res.total == pytest.approx(1999 * 1.08875, abs=0.01)
    assert fake.orders == [[MODEL.part]] and fake.store == "R095"
    lock = json.loads((state_dir / "order.lock").read_text())
    assert lock["state"] == "confirmed" and lock["part"] == MODEL.part


def test_never_orders_twice(browser):
    fake = FakeApple()
    run(open_page(browser, fake), pickup_plan())
    with pytest.raises(CheckoutStopped, match="already placed"):
        run(open_page(browser, fake), pickup_plan())
    assert len(fake.orders) == 1 and fake.items == [MODEL.part]  # second phone never got past the bag


def test_review_mode_stops_before_place_order(browser):
    fake = FakeApple()
    page = open_page(browser, fake)
    res = run(page, pickup_plan(submit=False))
    assert not res.placed and "Review" in page.title() and fake.orders == []


def test_total_over_cap_stops(browser):
    fake = FakeApple(tax_rate=0.20)
    with pytest.raises(CheckoutStopped, match="over the"):
        run(open_page(browser, fake), pickup_plan())
    assert fake.orders == []


def test_two_phones_in_bag_trips_the_cap(browser):
    fake = FakeApple(extra_qty=1)
    with pytest.raises(CheckoutStopped, match="over the"):
        run(open_page(browser, fake), pickup_plan())
    assert fake.orders == []


def test_cvv_request_stops_and_report_has_no_values(browser):
    fake = FakeApple(cvv=True)
    with pytest.raises(CheckoutStopped, match="security code") as ei:
        run(open_page(browser, fake), pickup_plan())
    assert fake.orders == []
    report = ei.value.report.read_text()
    assert "SECRET123" not in report and "cc-csc" in report


def test_signin_stops(browser):
    with pytest.raises(CheckoutStopped, match="sign in"):
        run(open_page(browser, FakeApple(signin=True)), pickup_plan())


def test_store_not_offered_stops(browser):
    fake = FakeApple(stores=("R001",))
    with pytest.raises(CheckoutStopped, match="not offered"):
        run(open_page(browser, fake), pickup_plan())
    assert fake.orders == []


def test_delivery_rehearsal_reaches_review_without_ordering(browser):
    fake = FakeApple()
    res = run(open_page(browser, fake), Plan(model=MODEL, fulfillment="delivery", max_total=3000))
    assert not res.placed and res.total > MODEL.price and fake.orders == []


def test_pickup_rehearsal_picks_first_store(browser):
    fake = FakeApple()
    page = open_page(browser, fake)
    res = run(page, Plan(model=MODEL, fulfillment="pickup", check_model_name=False))
    assert res.store == "Fifth Avenue" and not res.placed


def test_empty_bag(browser):
    fake = FakeApple(extra_qty=2)
    page = open_page(browser, fake)
    assert len(fake.items) == 3
    assert empty_bag(page) == 3 and fake.items == []


def test_parse_total_ignores_subtotal():
    assert parse_total("Subtotal $1,999.00\nEstimated Tax $177.41\nTotal $2,176.41") == 2176.41
    assert parse_total("Subtotal $1,999.00") is None
    assert parse_total("Order Total: $ 2,000") == 2000.0


def test_rehearsal_gate(state_dir):
    ok, why = checkout.rehearsal_ok()
    assert not ok and "no rehearsal" in why
    checkout.record_rehearsal(False, "x")
    assert not checkout.rehearsal_ok()[0]
    checkout.record_rehearsal(True, "x")
    assert checkout.rehearsal_ok()[0]
    stale = {"at": (datetime.now() - timedelta(days=8)).isoformat(), "ok": True}
    (state_dir / "rehearsal.json").write_text(json.dumps(stale))
    assert not checkout.rehearsal_ok()[0]


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, title, message, **kw):
        self.sent.append((title, message))
        return True


def _cfg(tmp_path, mode):
    from duo_tracker.config import load
    p = tmp_path / "c.yaml"
    p.write_text(f"models: [256gb-nightsky]\ncheckout: {{mode: {mode}}}\n")
    return load(p)


def _hit_page(browser, fake):
    ctx = browser.new_context()
    ctx.route("**/*", fake.handle)
    page = ctx.new_page()
    page.goto("https://www.apple.com/shop/buy-iphone/iphone-duo")
    return page


def _store(number="R095", name="Fifth Avenue", miles=1.0):
    from duo_tracker.availability import StoreStock
    return StoreStock(name, number, "New York", f"{miles} mi", miles, True, "Today")


def test_watcher_hit_submit_mode_places_order_and_notifies(browser, tmp_path):
    from duo_tracker.cli import _act_on_hit
    fake, notes = FakeApple(), Notes()
    # two stores in stock: it should pick the nearest one
    _act_on_hit(_hit_page(browser, fake), _cfg(tmp_path, "submit"), notes, MODEL,
                [_store("R001", "SoHo", 3.0), _store("R095", "Fifth Avenue", 0.8)])
    assert fake.orders == [[MODEL.part]] and fake.store == "R095"
    assert notes.sent[-1][0] == "✅ iPhone Duo ORDER PLACED" and "W123456789" in notes.sent[-1][1]


def test_watcher_hit_review_mode_does_not_order(browser, tmp_path):
    from duo_tracker.cli import _act_on_hit
    fake, notes = FakeApple(), Notes()
    _act_on_hit(_hit_page(browser, fake), _cfg(tmp_path, "review"), notes, MODEL, [_store()])
    assert fake.orders == [] and notes.sent[-1][0].startswith("Review page ready")


def test_watcher_hit_stop_notifies_you(browser, tmp_path):
    from duo_tracker.cli import _act_on_hit
    fake, notes = FakeApple(cvv=True), Notes()
    _act_on_hit(_hit_page(browser, fake), _cfg(tmp_path, "submit"), notes, MODEL, [_store()])
    assert fake.orders == [] and notes.sent[-1][0] == "⚠️ Checkout needs you NOW"


def test_checkout_reports_step_timings(browser):
    from duo_tracker.checkout import format_timings
    res = run(open_page(browser, FakeApple()), pickup_plan())
    steps = [s for s, _ in res.timings]
    assert steps[0] == "check out" and "fulfillment" in steps and "review verified" in steps
    assert steps[-1] == "order confirmed"
    assert all(b >= a for (_, a), (_, b) in zip(res.timings, res.timings[1:]))
    assert "total" in format_timings(res.timings)


def test_stopped_checkout_carries_timings(browser):
    with pytest.raises(CheckoutStopped) as ei:
        run(open_page(browser, FakeApple(cvv=True)), pickup_plan())
    assert [s for s, _ in ei.value.timings][:2] == ["check out", "fulfillment"]


def _account_route(signed_in):
    def handle(route):
        path = urlparse(route.request.url).path
        if path == "/shop/account/home" and not signed_in:
            return route.fulfill(status=200, content_type="text/html",
                                 body="<script>location.replace('/signin')</script>")
        if path == "/signin":
            return route.fulfill(status=200, content_type="text/html",
                                 body="<input type=email><input type=password>")
        return route.fulfill(status=200, content_type="text/html", body="<h1>Your Account</h1>")
    return handle


@pytest.mark.parametrize("state", [True, False])
def test_signed_in_check(browser, state):
    from duo_tracker.checkout import signed_in
    ctx = browser.new_context()
    ctx.route("**/*", _account_route(state))
    assert signed_in(ctx.new_page()) is state
