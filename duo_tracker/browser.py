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
import time
from urllib.parse import unquote

from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError as PWTimeout

from .availability import Availability, matching_stores, parse  # noqa: F401 (re-export)
from .catalog import BAG_URL, BUY_URL, Model
from .config import Config

log = logging.getLogger(__name__)

BLOCKED_STATUSES = {403, 429, 503, 541}


class Blocked(Exception):
    """Apple refused the page's availability request."""


def open_context(pw: Playwright, cfg: Config) -> BrowserContext:
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(cfg.profile_dir), headless=False, no_viewport=True)
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


def check_model(page: Page, model: Model, carrier: str) -> Availability:
    """Load the buy page, select ``model``, and parse the page's own availability response."""
    page.goto(BUY_URL, wait_until="domcontentloaded", timeout=60_000)
    page.locator(f'[data-autom="dimensionColor{model.color}"]').wait_for(state="attached", timeout=30_000)

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


def go_to_bag(page: Page, model: Model, carrier: str, mode: str) -> str:
    """Add ``model`` to the bag and (optionally) open checkout. Returns a status line.

    The page is already on the buy page with ``model`` selected from check_model.
    Selectors past "Add to Bag" weren't verifiable from outside a real
    signed-in session, so every step degrades to "you take it from here".
    """
    try:
        page.locator('[data-autom="add-to-cart"]').click(timeout=15_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PWTimeout:
        page.bring_to_front()
        return "Couldn't click Add to Bag — the buy page is open with your model selected."

    if "/shop/bag" not in page.url:
        page.goto(BAG_URL, wait_until="domcontentloaded", timeout=60_000)
    status = "Added to bag."

    if mode == "checkout":
        btn = page.locator('[data-autom="checkout"]').or_(page.get_by_role("button", name="Check Out"))
        try:
            btn.first.click(timeout=15_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            status = "Added to bag and opened checkout — choose pickup at the store and pay."
        except PWTimeout:
            status = "Added to bag; couldn't find Check Out — tap it yourself."
    page.bring_to_front()
    return status



def sleep_with_status(seconds: float) -> None:
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(1.0, left))
