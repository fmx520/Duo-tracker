"""Drive a real browser on Apple's buy page and ask for stock from inside it.

Anti-bot evasion stack, ported from kelindi/apple-stock (MIT) plus the Lanbig
fork's additions:

  * Chrome launches with its automation tells switched off
    (``--disable-blink-features=AutomationControlled``, and Playwright's
    ``--enable-automation`` flag dropped), so the page can't trivially tell
    software is driving it.
  * The context presents a normal desktop-Chrome identity (user agent, locale,
    timezone) instead of automation defaults.
  * Availability comes from an **active in-page fetch** of
    ``/shop/fulfillment-messages``: the request runs in the page's own JS
    context -- same origin, same cookies -- carrying the page's internal
    ``x-aos-ui-fetch-call-1`` header with a fresh random value, plus a random
    cache-busting query id, so no two requests look identical.
  * On HTTP 541 (Apple's proof-of-work challenge not cleared yet) the round
    reloads the page and keeps retrying for up to 75 seconds instead of
    giving up on the first refusal.
  * Every 10th round asks about a model we don't want (decoy traffic), the
    check order is shuffled, and all timing is jittered, so the traffic reads
    more like a person browsing than a metronome.

It still never places an order unless ``checkout.mode`` says so, and every
checkout safety stop is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError as PWTimeout

from .availability import Availability, parse
from .catalog import BAG_URL, BUY_URL, MODELS, Model
from .config import Config

log = logging.getLogger(__name__)

BLOCKED_STATUSES = {403, 429, 503, 541}

# A normal desktop-Chrome user agent. Automation builds advertise themselves
# here ("HeadlessChrome" etc.), and Apple's shield rejects those.
REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# How long a round rides out a 541 (Apple's proof-of-work challenge) by
# reloading and retrying before it gives up and reports the block.
RETRY_BUDGET_SECONDS = 75

# How often (in watch rounds) to mix in a decoy request for a model we don't want.
DECOY_EVERY_ROUNDS = 10

# Apple marks its auth cookies (idmsa.apple.com "aa"/"aasp") as session-only
# (no expiry), so Chrome deletes them on exit and every run starts signed out
# of checkout. We snapshot them to disk while the browser is ALIVE and re-inject
# them with a real expiry at launch, so the login survives restarts.
#
# The snapshot must be taken while the context is live: a "close" event fires
# only after the browser is unreachable, so cookies read there come back empty.
_COOKIE_SNAPSHOT = "cookies.json"
_SESSION_COOKIE_MAX_AGE_DAYS = 30
_COOKIE_HOSTS = ["https://www.apple.com", "https://idmsa.apple.com",
                 "https://secure7.store.apple.com"]


class NoScreenError(RuntimeError):
    """Headless Linux with no (virtual) display: restarting won't fix it."""


class Blocked(Exception):
    """Apple refused the availability request."""


def _cookie_snapshot_path(cfg: Config) -> Path:
    return cfg.profile_dir.parent / _COOKIE_SNAPSHOT


def _restore_cookies(ctx: BrowserContext, cfg: Config) -> None:
    """Re-inject the cookies we snapshotted last run, as persistent cookies.

    Chrome drops session cookies (no expiry) when the browser closes, which is
    why Apple 'forgets' the sign-in between runs. We re-add them with a real
    expiry so they persist. Existing cookies win: we only fill gaps, so a fresh
    sign-in is never clobbered by a stale snapshot.
    """
    snap = _cookie_snapshot_path(cfg)
    if not snap.exists():
        return
    try:
        cookies = json.loads(snap.read_text())
    except Exception:
        return
    if not cookies:
        return
    try:
        existing = {(c["name"], c["domain"]) for c in ctx.cookies()}
    except Exception:
        existing = set()
    to_add = [c for c in cookies if (c["name"], c["domain"]) not in existing]
    if to_add:
        try:
            ctx.add_cookies(to_add)
            log.debug("restored %d session cookie(s)", len(to_add))
        except Exception as e:
            log.debug("cookie restore failed (ignored): %s", e)


def save_cookies(ctx: BrowserContext, cfg: Config) -> int:
    """Snapshot apple.com cookies (incl. session-only ones) for the next launch.

    MUST be called while the context is alive. Session cookies get a far-future
    expiry so Chrome treats them as persistent next time. Returns how many were
    saved; 0 means the browser was already gone and nothing was written.
    Never stores values in logs.
    """
    try:
        cookies = ctx.cookies(_COOKIE_HOSTS)
    except Exception as e:
        log.debug("cookie snapshot failed: %s", e)
        return 0
    if not cookies:
        return 0
    expiry = int(time.time()) + _SESSION_COOKIE_MAX_AGE_DAYS * 86400
    out = []
    for c in cookies:
        out.append({
            "name": c["name"], "value": c["value"], "domain": c["domain"],
            "path": c.get("path", "/"), "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True), "sameSite": c.get("sameSite", "Lax"),
            # -1 / missing expiry = session cookie; give it a real one.
            "expires": c["expires"] if c.get("expires", -1) and c["expires"] > 0 else expiry,
        })
    try:
        _cookie_snapshot_path(cfg).write_text(json.dumps(out))
        log.debug("snapshot %d cookie(s)", len(out))
    except Exception as e:
        log.debug("cookie snapshot write failed: %s", e)
        return 0
    return len(out)


def open_context(pw: Playwright, cfg: Config) -> BrowserContext:
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise NoScreenError(
            "no screen for the browser. On a headless Linux box, run commands through `./duo …` "
            "after `scripts/install-linux-service.sh` (it provides a virtual screen you can VNC into), "
            "or prefix with `xvfb-run -a`.")
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    # Playwright disables Chrome's sandbox by default. Keep it on: this browser
    # holds a signed-in Apple account and a saved card, so running it with process
    # isolation off is a poor trade. It is a local setting -- sites cannot observe
    # it either way, so this changes nothing about how we present ourselves.
    kwargs = dict(
        user_data_dir=str(cfg.profile_dir),
        headless=False,
        no_viewport=True,
        chromium_sandbox=True,
        # kelindi: present as ordinary desktop Chrome, not automation.
        user_agent=REAL_UA,
        locale="en-US",
        timezone_id=cfg.schedule_tz or "America/New_York",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    ctx = None
    if cfg.browser_channel and cfg.browser_channel != "chromium":
        try:
            ctx = pw.chromium.launch_persistent_context(channel=cfg.browser_channel, **kwargs)
        except Exception as e:  # channel not installed
            log.warning("could not launch %s (%s); falling back to bundled Chromium", cfg.browser_channel, e)
    if ctx is None:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    _restore_cookies(ctx, cfg)
    return ctx


def _fulfillment_path(part: str, zip_code: str = "") -> str:
    # Built by hand, not urlencode: part numbers contain "/" and Apple's own
    # page sends them unencoded, so our requests should look the same.
    params = [("fae", "true"), ("pl", "true"), ("mts.0", "regular"), ("parts.0", part)]
    if zip_code:
        params.append(("location", zip_code))
    # Lanbig: a random cache-busting id on every request, so Apple's anti-bot
    # system never sees two identical URLs.
    params.append(("_", f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"))
    qs = "&".join(f"{k}={v}" for k, v in params)
    return f"/shop/fulfillment-messages?{qs}"


def active_fetch(page: Page, part: str, zip_code: str = "") -> dict:
    """Fetch Apple's stock endpoint from *inside* the page (kelindi's key move).

    The request runs in the page's JavaScript context -- same origin, same
    cookies -- and carries the internal ``x-aos-ui-fetch-call-1`` header the
    buy page normally sends, with a fresh random value on every request
    (Lanbig). Raises Blocked on 541/403/429/503.
    """
    status, text = page.evaluate(
        """async (url) => {
            const r = await fetch(url, {
                credentials: 'include',
                headers: {'x-aos-ui-fetch-call-1': crypto.randomUUID()},
            });
            return [r.status, await r.text()];
        }""",
        _fulfillment_path(part, zip_code),
    )
    if status in BLOCKED_STATUSES:
        raise Blocked(f"HTTP {status}")
    if status != 200:
        raise RuntimeError(f"unexpected HTTP {status} from availability request")
    try:
        return json.loads(text)
    except Exception:
        raise Blocked("availability response was not JSON (likely a challenge page)")


def decoy_fetch(page: Page, watched: list[Model], zip_code: str = "") -> None:
    """Lanbig's decoy: ask about a model we don't want, so the request stream
    looks like someone browsing colors instead of polling a fixed list."""
    pool = [m for m in MODELS.values() if m.part not in {w.part for w in watched}]
    if not pool:
        return
    pick = random.choice(pool)
    try:
        active_fetch(page, pick.part, zip_code)
        log.debug("decoy check: %s", pick.key)
    except Exception as e:
        log.debug("decoy check failed (ignored): %s", e)


def _page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _check(page: Page, autom: str) -> None:
    page.locator(f'[data-autom="{autom}"]').check(force=True, timeout=15_000)


def select_config(page: Page, model: Model, carrier: str) -> None:
    for autom in (
        f"dimensionColor{model.color}",
        f"dimensionCapacity{model.capacity}",
        "choose-noTradeIn",
        "purchaseGroupOptionfullprice",
        f"carrierModel{carrier}",
        "noapplecare",
    ):
        _check(page, autom)
        page.wait_for_timeout(400)


def load_buy_page(page: Page) -> None:
    page.goto(BUY_URL, wait_until="domcontentloaded", timeout=60_000)
    page.locator('[data-autom^="dimensionColor"]').first.wait_for(state="attached", timeout=30_000)


def check_model(page: Page, model: Model, carrier: str, *, reload: bool = True,
                zip_code: str = "") -> Availability:
    """Select ``model`` on the buy page, then ask Apple for its availability
    with an active in-page fetch and parse the answer.

    With ``reload=False`` it reuses the already-loaded buy page, so a round
    over several models costs one page load, not one per model.
    """
    if reload:
        load_buy_page(page)
    select_config(page, model, carrier)
    return parse(active_fetch(page, model.part, zip_code), model.part)


def check_round(page: Page, models: list[Model], carrier: str, zip_code: str = "",
                retry_budget: int = RETRY_BUDGET_SECONDS):
    """Yield (model, Availability | Exception) for each model, loading the page once.

    On HTTP 541 the round reloads the buy page -- a fresh load re-runs Apple's
    proof-of-work challenge -- and retries for up to ``retry_budget`` seconds
    (kelindi's loop) before reporting the block. Model checks are spaced by a
    small random pause so the rhythm isn't metronomic (Lanbig).
    """
    deadline = time.monotonic() + retry_budget
    for i, m in enumerate(models):
        if i:
            time.sleep(random.uniform(0.8, 2.5))
        need_load = i == 0
        while True:
            try:
                yield m, check_model(page, m, carrier, reload=need_load, zip_code=zip_code)
                break
            except Blocked as e:
                if "541" not in str(e) or time.monotonic() >= deadline:
                    raise
                log.warning("Apple answered 541; reloading the page and retrying (%.0fs left)",
                            max(0.0, deadline - time.monotonic()))
                try:
                    load_buy_page(page)
                except Exception as reload_err:
                    log.debug("page reload during 541 retry failed: %s", reload_err)
                need_load = False
                time.sleep(random.uniform(2, 4))
            except Exception as e:
                yield m, e
                break


def go_to_bag(page: Page) -> bool:
    """Click Add to Bag on the buy page (``model`` already selected) and open the bag."""
    try:
        page.locator('[data-autom="add-to-cart"]').click(timeout=15_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PWTimeout:
        page.bring_to_front()
        return False
    if "/shop/bag" not in page.url:
        page.goto(BAG_URL, wait_until="domcontentloaded", timeout=60_000)
    page.bring_to_front()
    return True


def sleep_with_status(seconds: float, on_tick=None) -> None:
    end = time.monotonic() + seconds
    last = 0.0
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(1.0, left))
        if on_tick and time.monotonic() - last >= 5:
            last = time.monotonic()
            on_tick()
