"""Automated checkout: bag -> pickup at the in-stock store -> saved card -> place order.

Runs in the same visible, signed-in browser as the watcher. The point is speed,
not stealth. Every rule below makes it STOP and hand you the page instead of
guessing:

  * Apple asks you to sign in again / 2FA           -> stop (never automated)
  * Apple asks for a card security code or a card   -> stop (no card data stored)
  * Any page it doesn't recognise / a challenge     -> stop
  * Review page doesn't show the expected model     -> stop
  * Review page doesn't show the expected store     -> stop
  * Order total missing or above the cap            -> stop (also catches qty > 1)
  * An order was already placed by this tool        -> never submit again
  * No passing rehearsal in the last 7 days         -> never submit

When it stops, it saves a report of the page's buttons (labels and attributes
only, never field values) to ~/.duo-tracker/reports/ so selectors can be fixed.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import Locator, Page

from .catalog import BAG_URL, Model

STATE_DIR = Path.home() / ".duo-tracker"
REHEARSAL_MAX_AGE = timedelta(days=7)
MAX_CONTINUE_STEPS = 10
STEP_TIMEOUT_S = 15.0

# ── what we look for ──────────────────────────────────────────────────────────
CHECKOUT_BTN = ['[data-autom="checkout"]', re.compile(r"^\s*Check\s*Out\s*$", re.I)]
PICKUP_BTN = ['[data-autom*="fulfillment-pickup" i]', re.compile(r"pick\s*(it\s*)?up", re.I)]
DELIVERY_BTN = ['[data-autom*="fulfillment-delivery" i]', re.compile(r"(have it )?deliver", re.I)]
CONTINUE_BTN = ['[data-autom*="continue-button" i]',
                re.compile(r"^\s*(Continue|Review)(?!\s*(Shopping|as\s+Guest))", re.I)]
PLACE_ORDER_BTN = ['[data-autom*="place-order" i]', re.compile(r"Place\s+(Your\s+)?Order", re.I)]
REMOVE_BTN = ['[data-autom*="remove" i]', re.compile(r"^\s*Remove\b", re.I)]
WINDOW_SELECT = 'select[data-autom*="window" i], select[name*="window" i], select[id*="window" i]'

SIGNIN_HINTS = ("idmsa.apple.com", "/signin", "/sign-in")
CVV_FIELD = ('input[autocomplete="cc-csc"], input[name*="cvv" i], input[id*="cvv" i], '
             'input[name*="securitycode" i], input[id*="securitycode" i], input[aria-label*="security code" i]')
CARD_FIELD = 'input[autocomplete="cc-number"], input[name*="cardnumber" i], input[id*="cardnumber" i]'
CONFIRM_TEXT = re.compile(r"thank you for your (order|purchase)|your order number|order (is )?(placed|confirmed)", re.I)


class CheckoutStopped(Exception):
    def __init__(self, step: str, reason: str, report: Path | None = None):
        super().__init__(f"{step}: {reason}")
        self.step, self.reason, self.report = step, reason, report
        self.timings: list[tuple[str, float]] = []


@dataclass
class Plan:
    model: Model
    fulfillment: str            # "pickup" | "delivery"
    store_number: str = ""      # pickup only; "" = first store offered (rehearsal)
    store_name: str = ""
    max_total: float | None = None
    submit: bool = False
    check_model_name: bool = True


@dataclass
class Result:
    placed: bool
    total: float | None
    store: str
    confirmation: str = ""
    timings: list = field(default_factory=list)  # [(step, seconds since checkout start)]


def format_timings(timings) -> str:
    out, prev = [], 0.0
    for step, t in timings:
        out.append(f"{step} +{t - prev:.1f}s")
        prev = t
    return " → ".join(out) + (f"  (total {timings[-1][1]:.1f}s)" if timings else "")


# ── state files ───────────────────────────────────────────────────────────────
def lock_path() -> Path:
    return STATE_DIR / "order.lock"


def rehearsal_path() -> Path:
    return STATE_DIR / "rehearsal.json"


def order_lock() -> dict | None:
    p = lock_path()
    return json.loads(p.read_text()) if p.exists() else None


def rehearsal_ok() -> tuple[bool, str]:
    p = rehearsal_path()
    if not p.exists():
        return False, "no rehearsal yet — run `python -m duo_tracker rehearse`"
    data = json.loads(p.read_text())
    when = datetime.fromisoformat(data["at"])
    if not data.get("ok"):
        return False, "last rehearsal failed — fix and rerun `rehearse`"
    if datetime.now() - when > REHEARSAL_MAX_AGE:
        return False, f"last passing rehearsal was {when:%b %d} (>7 days) — rerun `rehearse`"
    return True, f"rehearsal passed {when:%b %d %H:%M}"


def record_rehearsal(ok: bool, detail: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rehearsal_path().write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                            "ok": ok, "detail": detail}))


def _write_lock(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path().write_text(json.dumps(data, indent=2))


# ── page helpers ──────────────────────────────────────────────────────────────
def _candidates(page: Page, specs) -> list[Locator]:
    out = []
    for s in specs:
        if isinstance(s, str):
            out.append(page.locator(s))
        else:
            out.append(page.get_by_role("button", name=s))
            out.append(page.get_by_role("link", name=s))
    return out


def find_visible(page: Page, specs, timeout: float = STEP_TIMEOUT_S) -> Locator | None:
    deadline = time.monotonic() + timeout
    while True:
        for loc in _candidates(page, specs):
            try:
                n = loc.count()
            except Exception:
                n = 0
            for i in range(min(n, 5)):
                el = loc.nth(i)
                try:
                    if el.is_visible() and el.is_enabled():
                        return el
                except Exception:
                    pass
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(150)


def _visible(page: Page, selector: str) -> bool:
    loc = page.locator(selector)
    try:
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
    except Exception:
        return False


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


_TOTAL_RE = re.compile(r"(?<![Ss]ub)(?<![Ss]ub )Total\b[^$\d]{0,40}\$\s?([\d,]+(?:\.\d{2})?)")


def parse_total(text: str) -> float | None:
    """Largest "Total $X" on the page (ignores "Subtotal"). None if absent."""
    vals = []
    for m in _TOTAL_RE.finditer(text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return max(vals) if vals else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def save_report(page: Page, step: str, reason: str) -> Path | None:
    """Dump the page's controls (labels/attributes only — never input values)."""
    try:
        controls = page.evaluate("""() => [...document.querySelectorAll(
              'button, a[role=button], a[href*="checkout"], input, select, label, [data-autom]')]
            .filter(e => e.offsetParent !== null)
            .slice(0, 400)
            .map(e => ({
              tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
              autom: e.getAttribute('data-autom') || '', aria: e.getAttribute('aria-label') || '',
              autocomplete: e.getAttribute('autocomplete') || '',
              text: (e.tagName === 'INPUT' || e.tagName === 'SELECT') ? '' :
                    (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
            }))""")
    except Exception:
        controls = []
    reports = STATE_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', step.lower())}.json"
    url = page.url.split("?")[0]
    path.write_text(json.dumps({"step": step, "reason": reason, "url": url, "title": page.title(),
                                "controls": controls}, indent=1))
    return path


def guard(page: Page, step: str) -> None:
    """Stop on anything we must never automate."""
    url = page.url.lower()
    if any(h in url for h in SIGNIN_HINTS) or _visible(page, 'input[type="password"]') \
            or any("idmsa.apple.com" in (f.url or "") for f in page.frames):
        raise CheckoutStopped(step, "Apple wants you to sign in / verify — do it in the window")
    if _visible(page, CVV_FIELD):
        raise CheckoutStopped(step, "Apple is asking for the card security code — enter it and place the order")
    if _visible(page, CARD_FIELD):
        raise CheckoutStopped(step, "no saved card on file — add one in the window")


def _stop(page: Page, step: str, reason: str):
    raise CheckoutStopped(step, reason, save_report(page, step, reason))


def _click_and_settle(page: Page, el: Locator, *, advances: bool = True) -> None:
    """Click, then wait only as long as the page actually needs.

    Deliberately NOT waiting for "network idle": Apple's pages fire analytics
    beacons constantly, so that would burn the full timeout on every step.
    For a button that advances checkout we wait until the URL changes or the
    button goes away (so we never double-click it); then the caller polls for
    the next button, which is the real readiness signal.
    """
    url = page.url
    el.click(timeout=10_000)
    if not advances:
        page.wait_for_timeout(120)
        return
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            if page.url != url or not el.is_visible():
                break
        except Exception:
            break  # element detached: page moved on
        page.wait_for_timeout(50)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass


def find_first(page: Page, options: dict, timeout: float = STEP_TIMEOUT_S):
    """Poll for whichever of several button kinds appears first -> (key, locator) or (None, None)."""
    deadline = time.monotonic() + timeout
    while True:
        for key, specs in options.items():
            el = find_visible(page, specs, timeout=0)
            if el is not None:
                return key, el
        if time.monotonic() >= deadline:
            return None, None
        page.wait_for_timeout(100)


# ── bag ───────────────────────────────────────────────────────────────────────
def empty_bag(page: Page) -> int:
    """Remove everything from the bag. Returns how many items were removed."""
    page.goto(BAG_URL, wait_until="domcontentloaded", timeout=60_000)
    removed = 0
    for _ in range(10):
        btn = find_visible(page, REMOVE_BTN, timeout=3)
        if not btn:
            break
        _click_and_settle(page, btn)
        removed += 1
    return removed


# ── the flow ──────────────────────────────────────────────────────────────────
def run(page: Page, plan: Plan) -> Result:
    """Drive checkout from the bag page. Raises CheckoutStopped on any doubt."""
    marks: list[tuple[str, float]] = []
    t0 = time.monotonic()
    mark = lambda name: marks.append((name, round(time.monotonic() - t0, 2)))  # noqa: E731
    try:
        res = _run(page, plan, mark)
        res.timings = marks
        return res
    except CheckoutStopped as e:
        if e.report is None:
            e.report = save_report(page, e.step, e.reason)
        e.timings = marks
        raise


def _run(page: Page, plan: Plan, mark=lambda name: None) -> Result:
    if plan.submit and order_lock():
        _stop(page, "start", "an order was already placed by this tool "
                             "(delete ~/.duo-tracker/order.lock to allow another)")
    if "/shop/bag" not in page.url:
        page.goto(BAG_URL, wait_until="domcontentloaded", timeout=60_000)

    # 1. Check Out
    guard(page, "bag")
    btn = find_visible(page, CHECKOUT_BTN)
    if not btn:
        _stop(page, "bag", "couldn't find Check Out")
    _click_and_settle(page, btn)
    mark("check out")

    # 2. Pickup or delivery (Apple may skip the chooser)
    guard(page, "fulfillment")
    choice = find_visible(page, PICKUP_BTN if plan.fulfillment == "pickup" else DELIVERY_BTN, timeout=10)
    if choice:
        # On Apple's checkout this reveals options in place rather than navigating.
        _click_and_settle(page, choice, advances=False)
    elif plan.fulfillment == "pickup":
        _stop(page, "fulfillment", "couldn't find the pickup option")

    store_label = plan.store_name
    if plan.fulfillment == "pickup":
        store_label = _choose_store(page, plan)
        _choose_window(page)
    mark("fulfillment")

    # 3. Click through Continue… until the review page
    for i in range(MAX_CONTINUE_STEPS):
        guard(page, f"step {i + 1}")
        kind, nxt = find_first(page, {"place": PLACE_ORDER_BTN, "continue": CONTINUE_BTN})
        if kind == "place":
            break
        if kind is None:
            _stop(page, f"step {i + 1}", "no Continue or Place Order button")
        _click_and_settle(page, nxt)
        mark(f"continue {i + 1}")
    else:
        _stop(page, "review", f"didn't reach the review page in {MAX_CONTINUE_STEPS} steps")

    # 4. Verify the review page before anything irreversible
    guard(page, "review")
    text = body_text(page)
    if plan.check_model_name and _norm(plan.model.name) not in _norm(text):
        _stop(page, "review", f"review page doesn't show '{plan.model.name}'")
    if plan.fulfillment == "pickup" and store_label and _norm(store_label) not in _norm(text):
        _stop(page, "review", f"review page doesn't show the store '{store_label}'")
    total = parse_total(text)
    if total is None:
        _stop(page, "review", "couldn't read the order total")
    if plan.max_total is not None and total > plan.max_total:
        _stop(page, "review", f"total ${total:,.2f} is over the ${plan.max_total:,.2f} cap")
    mark("review verified")

    if not plan.submit:
        return Result(placed=False, total=total, store=store_label)

    # 5. Place the order — at most once, ever, per lock file
    if order_lock():
        _stop(page, "review", "an order was already placed by this tool (delete ~/.duo-tracker/order.lock to allow another)")
    place = find_visible(page, PLACE_ORDER_BTN, timeout=3)
    if not place:
        _stop(page, "review", "Place Order button vanished")
    lock = {"state": "submitting", "at": datetime.now().isoformat(timespec="seconds"),
            "model": plan.model.name, "part": plan.model.part, "store": store_label, "total": total}
    _write_lock(lock)  # written BEFORE the click so a crash can never lead to a second order
    place.click(timeout=10_000)

    deadline = time.monotonic() + 45
    confirmation = ""
    while time.monotonic() < deadline:
        if "thankyou" in page.url.lower() or CONFIRM_TEXT.search(body_text(page)):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            for _ in range(20):  # order number can render a moment after the page
                m = re.search(r"\b(W\d{9,10})\b", body_text(page))
                if m:
                    confirmation = m.group(1)
                    break
                page.wait_for_timeout(250)
            break
        page.wait_for_timeout(500)
    mark("order confirmed" if confirmation or "thankyou" in page.url.lower() else "order submitted")
    lock.update(state="confirmed" if confirmation or "thankyou" in page.url.lower() else "unconfirmed",
                confirmation=confirmation)
    _write_lock(lock)
    return Result(placed=True, total=total, store=store_label, confirmation=confirmation)


def _choose_store(page: Page, plan: Plan) -> str:
    """Select the pickup store. Returns the store label to verify on review."""
    guard(page, "store")
    if plan.store_number:
        specs = [f'input[type="radio"][value="{plan.store_number}"]',
                 f'[data-autom*="{plan.store_number}"]']
        el = find_visible(page, specs, timeout=10)
        if el is None and plan.store_name:
            lbl = page.get_by_text(plan.store_name, exact=False)
            el = lbl.first if lbl.count() else None
        if el is None:
            _stop(page, "store", f"store {plan.store_name or plan.store_number} not offered at checkout")
        _click_and_settle(page, el, advances=False)
        return plan.store_name
    # rehearsal: take the first enabled store radio
    el = find_visible(page, ['input[type="radio"][name*="store" i]:not([disabled])'], timeout=10)
    if el is None:
        _stop(page, "store", "no pickup store offered")
    label = el.evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : '').split('\\n')[0].trim()")
    _click_and_settle(page, el, advances=False)
    return label or ""


def _choose_window(page: Page) -> None:
    sel = page.locator(WINDOW_SELECT)
    if not sel.count():
        return
    opts = sel.first.locator("option").evaluate_all(
        "os => os.filter(o => o.value && !o.disabled).map(o => o.value)")
    if opts:
        sel.first.select_option(opts[0])
        page.wait_for_timeout(300)


# ── sign-in check ─────────────────────────────────────────────────────────────
ACCOUNT_URL = "https://www.apple.com/shop/account/home"
ACCOUNT_HINTS = ("/shop/account", "/shop/orders")
# A real sign-in/2FA prompt, as opposed to Apple's redirect *through* the
# sign-in host: credential fields only. NOT input[type=tel] -- the signed-in
# account page renders a phone field for pickup contact, and treating that as
# a sign-in prompt is exactly the false negative this check is here to avoid.
SIGNIN_FIELDS = ('input[type="password"], input[name="accountName"], '
                 'input[id*="account_name" i], #remember-me')


def signed_in(page: Page) -> bool:
    """Load your Apple Store account page; False only if Apple leaves you at a
    real sign-in prompt.

    Signed-in sessions also get redirected *through* secureN.store.apple.com
    /shop/signIn/account?ssi=… -- it is how Apple refreshes the session and it
    forwards on to the account page a moment later. So a sign-in URL alone
    proves nothing: judge by where the redirect chain SETTLES and whether a
    credential form is actually on the page.
    """
    page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            url = page.url.lower()
        except Exception:
            page.wait_for_timeout(500)
            continue
        if any(h in url for h in SIGNIN_HINTS):
            # Sign-in host/path: only signed out if it STAYS here with a form.
            if _visible(page, SIGNIN_FIELDS):
                return False
            page.wait_for_timeout(500)
            continue
        if any(h in url for h in ACCOUNT_HINTS):
            return True
        # Some other apple.com page: a credential form means signed out;
        # otherwise give the redirect another beat to finish.
        if _visible(page, SIGNIN_FIELDS):
            return False
        page.wait_for_timeout(500)
    # Timed out mid-redirect: treat "no credential form anywhere" as signed in,
    # matching the watcher's don't-cry-wolf rule.
    return not _visible(page, SIGNIN_FIELDS)
