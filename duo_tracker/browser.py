"""Drive a real, visible browser on Apple's buy page and read what the page fetches.

Ground rules this module sticks to on purpose:
  * It uses YOUR browser profile, visible on screen, at a fixed polite interval.
  * It never calls Apple's APIs directly and has no stealth/fingerprint tricks.
  * If Apple refuses (541/403/429 or a challenge), it backs off and tells you.
    It does not try to get around the block.
  * It never places an order. At most it adds to bag and opens checkout;
    you confirm and pay.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from urllib.parse import unquote

from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError as PWTimeout

from .availability import Availability, parse
from .catalog import BAG_URL, BUY_URL, Model
from .config import Config

log = logging.getLogger(__name__)

BLOCKED_STATUSES = {403, 429, 503, 541}


class NoScreenError(RuntimeError):
    """Headless Linux with no (virtual) display: restarting won't fix it."""


class Blocked(Exception):
    """Apple refused the page's availability request."""


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
    kwargs = dict(user_data_dir=str(cfg.profile_dir), headless=False, no_viewport=True,
                  chromium_sandbox=True)
    if cfg.browser_channel and cfg.browser_channel != "chromium":
        try:
            return pw.chromium.launch_persistent_context(channel=cfg.browser_channel, **kwargs)
        except Exception as e:  # channel not installed
            log.warning("could not launch %s (%s); falling back to bundled Chromium", cfg.browser_channel, e)
    return pw.chromium.launch_persistent_context(**kwargs)


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


def check_model(page: Page, model: Model, carrier: str, *, reload: bool = True) -> Availability:
    """Select ``model`` and parse the availability response the page fetches for it.

    With ``reload=False`` it reuses the already-loaded buy page: changing the
    color/capacity makes the page ask Apple about the new part by itself, so a
    round over several models costs one page load, not one per model.
    """
    if reload:
        load_buy_page(page)

    def is_ours(r) -> bool:
        u = unquote(r.url)
        return "/shop/fulfillment-messages" in u and model.part in u

    try:
        with page.expect_response(is_ours, timeout=30_000) as info:
            select_config(page, model, carrier)
    except PWTimeout:
        raise RuntimeError("the page never asked Apple for availability (layout may have changed)")

    resp = info.value
    if resp.status in BLOCKED_STATUSES:
        raise Blocked(f"HTTP {resp.status}")
    if resp.status != 200:
        raise RuntimeError(f"unexpected HTTP {resp.status} from availability request")
    try:
        payload = resp.json()
    except Exception:
        raise Blocked("availability response was not JSON (likely a challenge page)")
    return parse(payload, model.part)


def check_round(page: Page, models: list[Model], carrier: str):
    """Yield (model, Availability | Exception) for each model, loading the page once."""
    for i, m in enumerate(models):
        try:
            yield m, check_model(page, m, carrier, reload=(i == 0))
        except Blocked:
            raise
        except Exception as first:
            if i == 0:
                yield m, first
                continue
            try:  # same-page switch didn't fire a request; try once more with a fresh load
                yield m, check_model(page, m, carrier, reload=True)
            except Blocked:
                raise
            except Exception as e:
                yield m, e


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
