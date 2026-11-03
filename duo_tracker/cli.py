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


def _report(model, av, stores) -> None:
    if not av.has_pickup_data:
        print(f"[{_ts()}] {model.name}: no store data — run `setup` and pick a store")
    else:
        print(f"[{_ts()}] {model.name}: {len(stores)}/{len(av.stores)} stores have it"
              + (f" ({', '.join(s.store_name for s in stores)})" if stores else ""))
    extra = [q for q in (av.delivery_quote, av.courier_quote if av.courier_available else "") if q]
    if extra:
        print(f"           {' | '.join(extra)}")


def cmd_check(args) -> int:
    from playwright.sync_api import sync_playwright

    from .availability import matching_stores
    from .browser import Blocked, check_round, open_context, _page
    from .config import load

    cfg = load(args.config)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        try:
            for m, res in check_round(page, cfg.models, cfg.carrier):
                if isinstance(res, Exception):
                    print(f"[{_ts()}] {m.name}: error: {res}")
                else:
                    _report(m, res, matching_stores(res, cfg.store_filter, cfg.max_miles))
        except Blocked as e:
            print(f"[{_ts()}] Apple blocked the availability request ({e}). Try again later.")
            return 2
        finally:
            ctx.close()
    return 0


def cmd_watch(args) -> int:
    from playwright.sync_api import sync_playwright

    from .availability import hit_keys, matching_stores
    from .browser import Blocked, check_round, open_context, _page, sleep_with_status
    from .checkout import empty_bag, order_lock, rehearsal_ok
    from .config import load

    cfg = load(args.config)
    notifier = Notifier(cfg.ntfy_server, cfg.ntfy_topic)
    if not cfg.ntfy_topic:
        print("warning: no ntfy.topic set — alerts will only print here.")
    if cfg.checkout_mode == "submit":
        if (lock := order_lock()):
            print(f"Refusing to auto-buy: this tool already placed an order ({lock.get('model')} at "
                  f"{lock.get('store')}, {lock.get('at')}). Delete ~/.duo-tracker/order.lock to allow another.")
            return 1
        ok, why = rehearsal_ok()
        if not ok:
            print(f"Refusing to auto-buy: {why}.")
            return 1
        print(f"Auto-buy armed ({why}). Caps: " +
              ", ".join(f"{m.key} ≤ ${cfg.cap_for(m):,.2f}" for m in cfg.models))

    print(f"Watching {', '.join(m.key for m in cfg.models)} every {cfg.interval_seconds}s "
          f"(checkout mode: {cfg.checkout_mode}). Ctrl-C to stop.")
    backoff = cfg.blocked_backoff_seconds
    warned_no_store = False
    errors_in_a_row = 0
    live: dict[str, str] = {}  # what was in stock last round, so we alert on new drops only

    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        if cfg.checkout_mode in ("review", "submit"):
            n = empty_bag(page)
            if n:
                print(f"Emptied {n} item(s) from your bag so checkout only contains the phone.")
        try:
            while True:
                now: dict[str, str] = {}
                checked: set[str] = set()
                try:
                    for m, av in check_round(page, cfg.models, cfg.carrier):
                        if isinstance(av, Exception):
                            errors_in_a_row += 1
                            print(f"[{_ts()}] {m.name}: error: {av}")
                            if errors_in_a_row == 5:
                                notifier.send("Duo-tracker needs attention", f"5 errors in a row; last: {av}",
                                              urgent=False, tags="warning")
                            continue
                        errors_in_a_row = 0
                        checked.add(m.part)
                        stores = matching_stores(av, cfg.store_filter, cfg.max_miles)
                        _report(m, av, stores)
                        if not av.has_pickup_data and not warned_no_store:
                            warned_no_store = True
                            notifier.send("Duo-tracker: no store set",
                                          "Run `python -m duo_tracker setup` and choose a pickup store.",
                                          urgent=False, tags="warning")

                        keys = hit_keys(m.part, av, stores, cfg.alert_on_courier)
                        now.update(keys)
                        fresh = [desc for k, desc in keys.items() if k not in live]
                        if not fresh:
                            continue

                        notifier.send(f"🚨 {m.name} IN STOCK", f"{'; '.join(fresh[:4])}. ${m.price}", click=BUY_URL)
                        fresh_stores = [st for st in stores if f"pickup:{st.store_number}:{m.part}" not in live]
                        if cfg.checkout_mode == "alert" or not fresh_stores:
                            continue  # courier-only drops are alert-only
                        _act_on_hit(page, cfg, notifier, m, fresh_stores)
                        print("\nStopped watching. Browser stays open — press Ctrl-C when done.")
                        while True:
                            time.sleep(60)
                except Blocked as e:
                    print(f"[{_ts()}] Apple blocked the request ({e}). Backing off {backoff // 60} min.")
                    notifier.send("Duo-tracker paused", f"Apple blocked checks ({e}). Retrying in {backoff // 60} min.",
                                  urgent=False, tags="warning")
                    sleep_with_status(backoff)
                    backoff = min(backoff * 2, 3600)
                    continue

                backoff = cfg.blocked_backoff_seconds
                # keep last-known state for models that errored this round, so they don't re-alert
                live = {k: v for k, v in live.items() if k.split(":")[-1] not in checked} | now
                sleep_with_status(cfg.interval_seconds)
        except KeyboardInterrupt:
            print("\nbye")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def _act_on_hit(page, cfg, notifier, model, stores) -> None:
    """Bag the phone and, depending on mode, drive checkout to review or place the order."""
    from .browser import go_to_bag
    from .checkout import CheckoutStopped, Plan, run

    bag_url = "https://www.apple.com/shop/bag"
    if not go_to_bag(page):
        notifier.send("Add to Bag failed — buy it NOW", f"{model.name}: the buy page is open with it selected.",
                      click=BUY_URL)
        return
    if cfg.checkout_mode == "bag":
        notifier.send("Finish your iPhone Duo order NOW", "It's in your bag — check out and pick up.", click=bag_url)
        return

    store = min(stores, key=lambda st: st.miles if st.miles is not None else 1e9)
    plan = Plan(model=model, fulfillment="pickup", store_number=store.store_number, store_name=store.store_name,
                max_total=cfg.cap_for(model), submit=cfg.checkout_mode == "submit")
    t0 = time.monotonic()
    try:
        res = run(page, plan)
    except CheckoutStopped as e:
        page.bring_to_front()
        print(f"[{_ts()}] checkout stopped at {e.step}: {e.reason}" + (f"\n           report: {e.report}" if e.report else ""))
        notifier.send("⚠️ Checkout needs you NOW", f"{e.reason}. Window is open at that step.", click=bag_url)
        return
    took = time.monotonic() - t0
    if res.placed:
        conf = f" Order {res.confirmation}." if res.confirmation else " (confirmation page not detected — check email)"
        msg = f"{model.name}, pickup at {res.store}, total ${res.total:,.2f}.{conf} ({took:.0f}s)"
        print(f"[{_ts()}] ✅ ORDER PLACED: {msg}")
        notifier.send("✅ iPhone Duo ORDER PLACED", msg, click="https://www.apple.com/shop/account/home")
    else:
        msg = f"{model.name} at {res.store}, total ${res.total:,.2f}. Tap Place Order. ({took:.0f}s)"
        notifier.send("Review page ready — PLACE ORDER NOW", msg, click=bag_url)


def cmd_rehearse(args) -> int:
    """Walk the full checkout without placing anything; arms `submit` mode on success."""
    from playwright.sync_api import sync_playwright

    from .browser import go_to_bag, load_buy_page, open_context, select_config, _page
    from .checkout import CheckoutStopped, Plan, empty_bag, record_rehearsal, run
    from .config import load

    cfg = load(args.config)
    model = cfg.models[0]
    results = []
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        try:
            empty_bag(page)
            print(f"1/2 delivery rehearsal with {model.name} (stops on the review page, never orders)…")
            load_buy_page(page)
            select_config(page, model, cfg.carrier)
            if not go_to_bag(page):
                raise CheckoutStopped("buy page", "couldn't click Add to Bag")
            r = run(page, Plan(model=model, fulfillment="delivery", max_total=cfg.cap_for(model)))
            results.append(f"delivery ok, review total ${r.total:,.2f}")
            print(f"    ✔ reached review — total ${r.total:,.2f} (cap ${cfg.cap_for(model):,.2f})")

            if args.pickup_item_url:
                print("2/2 pickup rehearsal with the accessory you gave (stops on review)…")
                empty_bag(page)
                page.goto(args.pickup_item_url, wait_until="domcontentloaded", timeout=60_000)
                if not go_to_bag(page):
                    raise CheckoutStopped("accessory page", "couldn't click Add to Bag")
                r = run(page, Plan(model=model, fulfillment="pickup", check_model_name=False))
                results.append(f"pickup ok at {r.store or 'first store'}")
                print(f"    ✔ pickup flow reached review at {r.store or 'the first store'} — total ${r.total:,.2f}")
            else:
                print("2/2 skipped pickup rehearsal (pass --pickup-item-url with any in-stock accessory to test it)")
        except CheckoutStopped as e:
            record_rehearsal(False, str(e))
            print(f"    ✘ stopped at {e.step}: {e.reason}")
            if e.report:
                print(f"      Send this file to fix the selectors: {e.report}")
            return 2
        finally:
            try:
                empty_bag(page)
            except Exception:
                pass
            ctx.close()
    record_rehearsal(True, "; ".join(results))
    print("Rehearsal passed — `checkout.mode: submit` is armed for 7 days.")
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
    sub.add_parser("watch", help="keep checking; act on stock per checkout.mode").set_defaults(fn=cmd_watch)
    r = sub.add_parser("rehearse", help="walk checkout to the review page without ordering (arms submit mode)")
    r.add_argument("--pickup-item-url", help="apple.com product page of any in-stock accessory, to test the pickup steps")
    r.set_defaults(fn=cmd_rehearse)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
