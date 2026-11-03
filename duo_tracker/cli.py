from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from .catalog import BUY_URL, CARRIERS, MODELS
from .notify import Notifier

log = logging.getLogger("duo_tracker")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def cmd_models(_args) -> int:
    print(f"{'key':<18}{'part':<12}{'price':>7}  name")
    for m in MODELS.values():
        print(f"{m.key:<18}{m.part:<12}{m.price:>7}  {m.name}")
    print(f"\ncarriers: {', '.join(CARRIERS)}")
    return 0


def cmd_notify_test(args) -> int:
    from .config import load

    cfg = load(args.config)
    if not cfg.ntfy_topic:
        print("Set ntfy.topic in your config first.")
        return 1
    ok = Notifier(cfg.ntfy_server, cfg.ntfy_topic).send(
        "Duo-tracker test", "If you see this on your phone, alerts work.", click=BUY_URL, urgent=False
    )
    print("sent" if ok else "FAILED — check the topic/server")
    return 0 if ok else 1


def cmd_setup(args) -> int:
    from playwright.sync_api import sync_playwright

    from .browser import open_context, _page
    from .config import load

    cfg = load(args.config)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        page.goto(BUY_URL)
        print(
            "\nA browser window opened. In it:\n"
            "  1. Sign in to your Apple Account (top-right bag icon → Sign in). Approve 2FA.\n"
            "  2. Pick any iPhone Duo config, click 'Check availability', enter your ZIP,\n"
            "     and select your preferred store. Apple remembers it in this profile.\n"
            "  3. Make sure a payment method is saved in your Apple Account (Apple Pay or card).\n"
            "  4. Close the browser window when done.\n"
        )
        ctx.wait_for_event("close", timeout=0)
    print(f"Saved to {cfg.profile_dir}. Next: python -m duo_tracker check")
    return 0


def _report(model, av) -> None:
    hits = av.in_stock_stores
    if not av.has_pickup_data:
        print(f"[{_ts()}] {model.name}: no store data — run `setup` and pick a store")
    else:
        print(f"[{_ts()}] {model.name}: {len(hits)}/{len(av.stores)} stores have it"
              + (f" ({', '.join(s.store_name for s in hits)})" if hits else ""))
    if av.delivery_quote:
        print(f"           delivery: {av.delivery_quote}")


def cmd_check(args) -> int:
    from playwright.sync_api import sync_playwright

    from .browser import Blocked, check_model, open_context, _page
    from .config import load

    cfg = load(args.config)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        for m in cfg.models:
            try:
                _report(m, check_model(page, m, cfg.carrier))
            except Blocked as e:
                print(f"[{_ts()}] Apple blocked the availability request ({e}). Try again later.")
                return 2
            except Exception as e:
                print(f"[{_ts()}] {m.name}: error: {e}")
        ctx.close()
    return 0


def cmd_watch(args) -> int:
    from playwright.sync_api import sync_playwright

    from .browser import Blocked, check_model, go_to_bag, matching_stores, open_context, _page, sleep_with_status
    from .config import load

    cfg = load(args.config)
    notifier = Notifier(cfg.ntfy_server, cfg.ntfy_topic)
    if not cfg.ntfy_topic:
        print("warning: no ntfy.topic set — alerts will only print here.")

    print(f"Watching {', '.join(m.key for m in cfg.models)} every {cfg.interval_seconds}s "
          f"(checkout mode: {cfg.checkout_mode}). Ctrl-C to stop.")
    backoff = cfg.blocked_backoff_seconds
    warned_no_store = False
    errors_in_a_row = 0

    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        try:
            while True:
                for m in cfg.models:
                    try:
                        av = check_model(page, m, cfg.carrier)
                    except Blocked as e:
                        print(f"[{_ts()}] Apple blocked the request ({e}). Backing off {backoff // 60} min.")
                        notifier.send("Duo-tracker paused", f"Apple blocked checks ({e}). Retrying in {backoff // 60} min.",
                                      urgent=False, tags="warning")
                        sleep_with_status(backoff)
                        backoff = min(backoff * 2, 3600)
                        break
                    except Exception as e:
                        errors_in_a_row += 1
                        print(f"[{_ts()}] {m.name}: error: {e}")
                        if errors_in_a_row == 5:
                            notifier.send("Duo-tracker needs attention", f"5 errors in a row; last: {e}",
                                          urgent=False, tags="warning")
                        continue

                    errors_in_a_row = 0
                    backoff = cfg.blocked_backoff_seconds
                    _report(m, av)
                    if not av.has_pickup_data and not warned_no_store:
                        warned_no_store = True
                        notifier.send("Duo-tracker: no store set",
                                      "Run `python -m duo_tracker setup` and choose a pickup store.",
                                      urgent=False, tags="warning")

                    stores = matching_stores(av, cfg.store_filter)
                    if not stores:
                        continue

                    where = "; ".join(f"{s.store_name} ({s.quote or 'available'})" for s in stores[:4])
                    notifier.send(f"🚨 {m.name} IN STOCK", f"Pickup: {where}. ${m.price}", click=BUY_URL)
                    if cfg.checkout_mode != "alert":
                        status = go_to_bag(page, m, cfg.carrier, cfg.checkout_mode)
                        notifier.send("Finish your iPhone Duo order NOW", status, click="https://www.apple.com/shop/bag")
                    print("\nStopped watching so you can finish checkout. Browser stays open — press Ctrl-C when done.")
                    while True:
                        time.sleep(60)
                else:
                    sleep_with_status(cfg.interval_seconds)
        except KeyboardInterrupt:
            print("\nbye")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="duo_tracker", description="Watch Apple for iPhone Duo pickup stock.")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="list model keys and part numbers").set_defaults(fn=cmd_models)
    sub.add_parser("setup", help="sign in and choose a store in the bot's browser profile").set_defaults(fn=cmd_setup)
    sub.add_parser("notify-test", help="send a test push to your ntfy topic").set_defaults(fn=cmd_notify_test)
    sub.add_parser("check", help="check each model once and print results").set_defaults(fn=cmd_check)
    sub.add_parser("watch", help="keep checking; alert + add to bag on stock").set_defaults(fn=cmd_watch)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
