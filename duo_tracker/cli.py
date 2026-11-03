from __future__ import annotations

import argparse
import logging
import random
import socket
import sys
import time
from datetime import datetime

from .catalog import BUY_URL, CARRIERS, MODELS
from .notify import Notifier

log = logging.getLogger("duo_tracker")


def say(msg: str, level: int = logging.INFO, exc: BaseException | None = None) -> None:
    """Screen + log file. With ``exc``, the full traceback goes to the log file only."""
    log.log(level, msg)
    if exc is not None:
        log.debug("details for: %s", msg, exc_info=(type(exc), exc, exc.__traceback__))

# Exit code for "won't fix itself by restarting" (bad config, order already
# placed, no rehearsal). The systemd unit is told not to restart on it.
EXIT_NO_RESTART = 3


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

    from .health import Health

    cfg = load(args.config)
    if not cfg.ntfy_topic:
        print("Set ntfy.topic in your config first.")
        return 1
    notifier = Notifier(cfg.ntfy_server, cfg.ntfy_topic)
    ok = notifier.send(
        "Duo-tracker test", "If you see this on your phone, alerts work.", click=BUY_URL, urgent=False
    )
    print("ntfy:        sent" if ok else "ntfy:        FAILED — check the topic/server")

    # The dead-man switch is the only alert that still fires when this box is the
    # thing that died, so check it here rather than find out during an outage.
    if not cfg.healthcheck_url:
        print("healthcheck: not set — if this box loses power or its connection, "
              "nothing will tell you. See health.healthcheck_url.")
    elif Health(notifier, daily_hour=None, healthcheck_url=cfg.healthcheck_url).ping(force=True):
        print("healthcheck: pinged — that check should now read 'up'")
    else:
        print("healthcheck: FAILED — check health.healthcheck_url")
        ok = False
    return 0 if ok else 1


def _ask(prompt: str, default: str = "", check=None, input_fn=input) -> str:
    while True:
        ans = input_fn(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip() or default
        if check is None:
            return ans
        try:
            check(ans)
            return ans
        except (ValueError, KeyError) as e:
            print(f"  ✘ {e}")


CONFIG_TEMPLATE = """# Written by `python -m duo_tracker init`. Edit freely; see config.example.yaml for every option.
zip: "{zip}"               # enter this in the bot's browser during `./duo setup`
models:
{models}
carrier: {carrier}
stores: [{stores}]
max_miles: {max_miles}
alert_on_courier: true

# Faster when drops land (Long Island: ~7:30–9:30am ET, some around midnight).
# A block inside a fast window drops that window back to default_seconds.
schedule:
  timezone: America/New_York
  default_seconds: 90
  windows:
    - {{start: "07:30", end: "09:30", seconds: 30}}
    - {{start: "23:30", end: "00:30", seconds: 30}}

ntfy:
  server: https://ntfy.sh
  topic: {topic}

checkout:
  mode: {mode}            # alert | bag | review | submit
  # max_total: 2250        # default cap = model price x 1.11

health:
  daily_summary_hour: 9    # daily "still watching" push at 9am; null to disable
  stall_minutes: 10
  healthcheck_url: "{hc}"

browser:
  channel: chrome
  profile_dir: ~/.duo-tracker/profile
"""


def cmd_init(args, input_fn=input) -> int:
    import secrets
    from pathlib import Path

    from .catalog import resolve

    from .catalog import ZIP_PRESETS

    path = Path(args.config)
    if path.exists() and _ask(f"{path} exists — overwrite? (y/n)", "n", input_fn=input_fn).lower() != "y":
        print("Left it alone.")
        return 0

    def valid_zip(a):
        if not (a.isdigit() and len(a) == 5):
            raise ValueError("enter a 5-digit ZIP code")
    zip_code = _ask("Your ZIP code", "11754", valid_zip, input_fn)
    preset = ZIP_PRESETS.get(zip_code, {})
    if preset:
        print(f"  Using the {zip_code} preset: nearby stores with a drop history, and the models that actually drop.")

    print("\nWhich iPhone Duo models do you want? (comma-separated numbers)")
    keys = list(MODELS)
    default_models = ",".join(str(keys.index(k) + 1) for k in preset.get("models", ["256gb-nightsky"]))
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {MODELS[k].name} — ${MODELS[k].price:,}")

    def pick(ans):
        nums = [int(x) for x in ans.replace(" ", "").split(",") if x]
        if not nums or any(not 1 <= n <= len(keys) for n in nums):
            raise ValueError(f"pick numbers between 1 and {len(keys)}")
    chosen = [keys[int(x) - 1] for x in _ask("Models", default_models, pick, input_fn).replace(" ", "").split(",") if x]
    for k in chosen:
        resolve(k)

    carrier = _ask(f"Carrier ({' / '.join(CARRIERS)})", "unlocked",
                   lambda a: CARRIERS[a.lower()], input_fn).lower()

    print("\nWhat should happen when one drops?\n"
          "  alert  – just notify me\n"
          "  bag    – put it in my bag\n"
          "  review – fill in checkout, I tap Place Order\n"
          "  submit – buy it automatically (needs a passing `rehearse` first)")
    def valid_mode(a):
        if a not in ("alert", "bag", "review", "submit"):
            raise ValueError("choose alert, bag, review or submit")
    mode = _ask("Mode", "submit", valid_mode, input_fn)

    max_miles = _ask("Ignore stores farther than how many miles?", str(preset.get("max_miles", 20)),
                     lambda a: float(a), input_fn)
    stores = _ask("Only these stores? (comma-separated names/numbers, blank = any nearby)",
                  ", ".join(preset.get("stores", [])), input_fn=input_fn)
    topic = _ask("ntfy topic (install the ntfy app and subscribe to this)", f"duo-{secrets.token_hex(5)}",
                 input_fn=input_fn)
    hc = _ask("healthchecks.io ping URL for 'box went offline' alerts (blank to skip)", "", input_fn=input_fn)

    store_list = ", ".join(f'"{x.strip()}"' for x in stores.split(",") if x.strip())
    path.write_text(CONFIG_TEMPLATE.format(
        models="\n".join(f"  - {k}" for k in chosen), carrier=carrier, stores=store_list,
        max_miles=max_miles, topic=topic, mode=mode, hc=hc, zip=zip_code))
    print(f"\n✔ wrote {path}")

    if _ask("Send a test notification now? (y/n)", "y", input_fn=input_fn).lower() == "y":
        cmd_notify_test(args)
    print("\nNext: sign in with `setup`, then `rehearse`, then `watch` (or start the service).")
    return 0


def cmd_setup(args) -> int:
    from playwright.sync_api import sync_playwright

    from .browser import open_context, _page
    from .config import load

    cfg = load(args.config)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        page.goto(BUY_URL)
        zip_hint = cfg.zip or "your ZIP"
        say(
            "\nA browser window opened. In it:\n"
            "  1. Sign in to your Apple Account (top-right bag icon → Sign in). Approve 2FA.\n"
            f"  2. Pick any iPhone Duo config, click 'Check availability', enter {zip_hint},\n"
            "     and select your preferred store. Apple remembers it in this profile.\n"
            "  3. Make sure a payment method is saved in your Apple Account (Apple Pay or card).\n"
            "  4. Close the browser window when done.\n"
        )
        ctx.wait_for_event("close", timeout=0)
    say(f"Saved to {cfg.profile_dir}. Next: python -m duo_tracker check")
    return 0


def _report(model, av, stores) -> None:
    if not av.has_pickup_data:
        say(f"{model.name}: no store data — run `setup` and pick a store")
    else:
        say(f"{model.name}: {len(stores)}/{len(av.stores)} stores have it"
              + (f" ({', '.join(s.store_name for s in stores)})" if stores else ""))
    extra = [q for q in (av.delivery_quote, av.courier_quote if av.courier_available else "") if q]
    if extra:
        say(f"           {' | '.join(extra)}")


def cmd_check(args) -> int:
    from playwright.sync_api import sync_playwright

    from .availability import matching_stores
    from .browser import Blocked, check_round, open_context, _page
    from .checkout import STATE_DIR
    from .config import load

    cfg = load(args.config)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        try:
            for m, res in check_round(page, cfg.models, cfg.carrier, zip_code=cfg.zip):
                if isinstance(res, Exception):
                    say(f"{m.name}: error: {res}")
                else:
                    _report(m, res, matching_stores(res, cfg.store_filter, cfg.max_miles))
        except Blocked as e:
            # Log it the way cmd_watch does, so `./duo blocks` sees one-off checks too.
            cfg.make_schedule(STATE_DIR / "blocks.csv").on_block(str(e))
            say(f"Apple blocked the availability request ({e}). Try again later.")
            return 2
        finally:
            ctx.close()
    return 0


def cmd_watch(args) -> int:
    from playwright.sync_api import sync_playwright

    from .availability import hit_keys, matching_stores
    from .browser import Blocked, check_round, decoy_fetch, open_context, _page, sleep_with_status
    from .checkout import empty_bag, order_lock, rehearsal_ok, signed_in
    from .checkout import STATE_DIR
    from .config import load
    from .health import Health

    cfg = load(args.config)
    if cfg.checkout_mode == "trigger":
        return _watch_triggers(cfg)
    sched = cfg.make_schedule(STATE_DIR / "blocks.csv")
    from .drops import DropLog
    drop_log = DropLog(STATE_DIR / "drops.jsonl")
    open_drops: dict[str, str] = {}      # availability key -> drop id, while it's in stock
    last_checked: dict[str, float] = {}  # part -> monotonic time of last successful check
    notifier = Notifier(cfg.ntfy_server, cfg.ntfy_topic)
    if not cfg.ntfy_topic:
        say("warning: no ntfy.topic set — alerts will only print here.")
    if cfg.checkout_mode == "submit":
        if (lock := order_lock()):
            say(f"Refusing to auto-buy: this tool already placed an order ({lock.get('model')} at "
                  f"{lock.get('store')}, {lock.get('at')}). Delete ~/.duo-tracker/order.lock to allow another.")
            return EXIT_NO_RESTART
        ok, why = rehearsal_ok()
        if ok:
            say(f"Auto-buy armed ({why}). Caps: " +
                  ", ".join(f"{m.key} ≤ ${cfg.cap_for(m):,.2f}" for m in cfg.models))
        else:
            # Keep watching rather than stopping: fill checkout and let you tap Place Order.
            cfg.checkout_mode = "review"
            say(f"Auto-buy NOT armed ({why}). Watching in review mode: it fills checkout, you tap Place Order.")
            notifier.send("Auto-buy not armed", f"{why}. Still watching. On a drop it fills checkout and "
                          "you tap Place Order. Run `./duo rehearse` to re-arm.", urgent=False, tags="warning")

    peaks = ", ".join(f"{w.label} every {w.seconds}s" for w in cfg.schedule_windows)
    say(f"Watching {', '.join(m.key for m in cfg.models)}: every {cfg.schedule_default}s"
          + (f", faster at peak ({peaks}, {cfg.schedule_tz})" if peaks else "")
          + f". Checkout mode: {cfg.checkout_mode}. Ctrl-C to stop.")
    last_label = ""
    backoff = cfg.blocked_backoff_seconds
    warned_no_store = False
    errors_in_a_row = 0
    rounds = 0
    live: dict[str, str] = {}  # what was in stock last round, so we alert on new drops only
    health = Health(notifier, daily_hour=cfg.daily_summary_hour, stall_minutes=cfg.stall_minutes,
                    healthcheck_url=cfg.healthcheck_url)
    needs_signin = cfg.checkout_mode in ("review", "submit")
    last_signin_check = 0.0
    signed_out_notified = False

    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        if cfg.checkout_mode in ("review", "submit"):
            n = empty_bag(page)
            if n:
                say(f"Emptied {n} item(s) from your bag so checkout only contains the phone.")
        notifier.send("Duo-tracker started", f"Watching on {socket.gethostname()} "
                      f"({', '.join(m.key for m in cfg.models)}; mode {cfg.checkout_mode}).",
                      urgent=False, tags="eyes")
        try:
            while True:
                if needs_signin and time.monotonic() - last_signin_check >= cfg.signin_check_minutes * 60:
                    last_signin_check = time.monotonic()
                    try:
                        ok = signed_in(page)
                    except Exception as e:
                        ok = True  # couldn't tell; don't cry wolf
                        say(f"sign-in check failed: {e}", logging.WARNING, exc=e)
                    if not ok and not signed_out_notified:
                        signed_out_notified = True
                        say("Signed out of Apple — sign back in (VNC into the box) or checkout will stop.",
                            logging.WARNING)
                        notifier.send("⚠️ Signed out of Apple", "Sign back in on the tracker's browser "
                                      "(VNC on the Linux box), or auto-checkout will stop at sign-in.", tags="warning")
                    elif ok and signed_out_notified:
                        signed_out_notified = False
                        notifier.send("Signed back in", "Auto-checkout is ready again.", urgent=False,
                                      tags="white_check_mark")
                now: dict[str, str] = {}
                checked: set[str] = set()
                try:
                    models = list(cfg.models)
                    random.shuffle(models)  # vary the poll order round to round
                    for m, av in check_round(page, models, cfg.carrier, zip_code=cfg.zip):
                        if isinstance(av, Exception):
                            errors_in_a_row += 1
                            health.problem(str(av))
                            say(f"{m.name}: error: {av}", logging.WARNING, exc=av)
                            if errors_in_a_row == 5:
                                notifier.send("Duo-tracker needs attention", f"5 errors in a row; last: {av}",
                                              urgent=False, tags="warning")
                            continue
                        errors_in_a_row = 0
                        health.success()
                        checked.add(m.part)
                        prev_check = last_checked.get(m.part)
                        last_checked[m.part] = time.monotonic()
                        gap = None if prev_check is None else last_checked[m.part] - prev_check
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

                        health.drop()
                        say(f"🚨 DROP: {m.name} — {'; '.join(fresh)}")
                        notifier.send(f"🚨 {m.name} IN STOCK", f"{'; '.join(fresh[:4])}. ${m.price}", click=BUY_URL)
                        by_number = {st.store_number: st for st in stores}
                        new_ids = {}
                        for k, desc in keys.items():
                            if k in live:
                                continue
                            num = k.split(":")[1] if k.startswith("pickup:") else ""
                            st = by_number.get(num)
                            new_ids[k] = drop_log.start(
                                key=k, model=m.name, part=m.part, store=st.store_name if st else desc,
                                store_number=num, kind="pickup" if num else "courier",
                                notice_gap_s=gap, window=sched.current()[1])
                        open_drops.update(new_ids)
                        fresh_stores = [st for st in stores if f"pickup:{st.store_number}:{m.part}" not in live]
                        if cfg.checkout_mode == "alert" or not fresh_stores:
                            for i in new_ids.values():  # courier-only drops are alert-only
                                drop_log.outcome(i, "alert only")
                            continue
                        result = _act_on_hit(page, cfg, notifier, m, fresh_stores)
                        for k, i in new_ids.items():
                            if result and k == f"pickup:{result['store_number']}:{m.part}":
                                drop_log.outcome(i, result["outcome"], result.get("detail", ""), result.get("checkout_s"))
                            else:
                                drop_log.outcome(i, "alert only", "another store was chosen" if result else "")
                        say("Stopped watching. Browser stays open — press Ctrl-C when done.")
                        while True:  # keep the dead-man switch fed so it doesn't report the box down
                            time.sleep(60)
                            health.ping()
                except Blocked as e:
                    say(f"Apple blocked the request ({e}). Backing off {backoff // 60} min.", logging.WARNING)
                    notifier.send("Duo-tracker paused", f"Apple blocked checks ({e}). Retrying in {backoff // 60} min.",
                                  urgent=False, tags="warning")
                    health.already_notified(f"Apple blocked checks ({e})")
                    sched.on_block(str(e))
                    sleep_with_status(backoff, on_tick=health.tick)
                    backoff = min(backoff * 2, 3600)
                    continue

                backoff = cfg.blocked_backoff_seconds
                # keep last-known state for models that errored this round, so they don't re-alert
                live = {k: v for k, v in live.items() if k.split(":")[-1] not in checked} | now
                for k in [k for k in open_drops if k not in live]:  # sold out since last round
                    drop_log.end(open_drops.pop(k))
                rounds += 1
                if rounds % 10 == 0:
                    # Lanbig: decoy request for a model we don't want, so the
                    # traffic looks more like a person browsing colors.
                    decoy_fetch(page, cfg.models, cfg.zip)
                health.tick()
                wait, label = sched.current()
                wait = max(30, int(wait * random.uniform(0.7, 1.3)))  # jitter the interval
                if label != last_label:
                    last_label = label
                    say(f"{label}: checking every ~{wait}s")
                sleep_with_status(wait, on_tick=health.tick)
        except KeyboardInterrupt:
            say("bye")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def _act_on_hit(page, cfg, notifier, model, stores) -> dict:
    """Bag the phone and, depending on mode, drive checkout to review or place the order.

    Returns {"outcome", "detail", "checkout_s", "store_number"} for the drop history.
    """
    from .browser import go_to_bag
    from .checkout import CheckoutStopped, Plan, format_timings, run

    bag_url = "https://www.apple.com/shop/bag"
    store = min(stores, key=lambda st: st.miles if st.miles is not None else 1e9)
    result = {"store_number": store.store_number, "detail": "", "checkout_s": None}
    try:
        if not go_to_bag(page):
            say("Add to Bag failed", logging.WARNING)
            notifier.send("Add to Bag failed — buy it NOW", f"{model.name}: the buy page is open with it selected.",
                          click=BUY_URL)
            return result | {"outcome": "add-to-bag failed"}
        if cfg.checkout_mode == "bag":
            notifier.send("Finish your iPhone Duo order NOW", "It's in your bag — check out and pick up.",
                          click=bag_url)
            return result | {"outcome": "bagged"}

        plan = Plan(model=model, fulfillment="pickup", store_number=store.store_number,
                    store_name=store.store_name, max_total=cfg.cap_for(model),
                    submit=cfg.checkout_mode == "submit")
        t0 = time.monotonic()
        try:
            res = run(page, plan)
        except CheckoutStopped as e:
            page.bring_to_front()
            say(f"checkout stopped at {e.step}: {e.reason}" + (f" — report: {e.report}" if e.report else ""),
                logging.WARNING)
            if e.timings:
                say(f"checkout timings: {format_timings(e.timings)}")
            notifier.send("⚠️ Checkout needs you NOW", f"{e.reason}. Window is open at that step.", click=bag_url)
            return result | {"outcome": "needs you", "detail": f"{e.step}: {e.reason}",
                             "checkout_s": time.monotonic() - t0}
        took = time.monotonic() - t0
        say(f"checkout timings: {format_timings(res.timings)}")
        if res.placed:
            conf = f" Order {res.confirmation}." if res.confirmation else " (confirmation page not detected — check email)"
            msg = f"{model.name}, pickup at {res.store}, total ${res.total:,.2f}.{conf} ({took:.0f}s)"
            say(f"✅ ORDER PLACED: {msg}")
            notifier.send("✅ iPhone Duo ORDER PLACED", msg, click="https://www.apple.com/shop/account/home")
            return result | {"outcome": "ordered", "detail": f"${res.total:,.2f}", "checkout_s": took}
        msg = f"{model.name} at {res.store}, total ${res.total:,.2f}. Tap Place Order. ({took:.0f}s)"
        say(f"Review page ready: {msg}")
        notifier.send("Review page ready — PLACE ORDER NOW", msg, click=bag_url)
        return result | {"outcome": "review ready", "checkout_s": took}
    except Exception as e:  # never let an unexpected page state kill the watcher mid-drop
        say(f"checkout hit an unexpected error: {e}", logging.ERROR, exc=e)
        try:
            page.bring_to_front()
        except Exception:
            pass
        notifier.send("⚠️ Checkout needs you NOW", f"Unexpected error: {str(e)[:120]}", click=bag_url)
        return result | {"outcome": "needs you", "detail": f"error: {str(e)[:200]}"}


# ── trigger mode (TRIGGER_MODE.md) ─────────────────────────────────────────────
REJECT_ALERT_EVERY_S = 300  # rejected triggers alert, but at most this often (flood guard)


def _hold_after_acting(health) -> None:
    """After a checkout attempt: stop acting, keep the dead-man switch fed."""
    say("Stopped listening. Browser stays open — press Ctrl-C when done.")
    while True:
        time.sleep(60)
        health.ping()


def _watch_triggers(cfg, listen_fn=None) -> int:
    """Wait for signed triggers; on one, confirm with Apple and run the normal checkout."""
    import dataclasses

    from playwright.sync_api import sync_playwright

    from . import trigger as trig
    from .browser import open_context, _page
    from .checkout import STATE_DIR, empty_bag, order_lock, rehearsal_ok, signed_in
    from .drops import DropLog
    from .health import Health

    notifier = Notifier(cfg.ntfy_server, cfg.ntfy_topic)
    if not cfg.ntfy_topic:
        say("warning: no ntfy.topic set — alerts will only print here.")
    if (lock := order_lock()):
        say(f"Refusing to run: this tool already placed an order ({lock.get('model')} at "
            f"{lock.get('store')}, {lock.get('at')}). Delete ~/.duo-tracker/order.lock to allow another.")
        return EXIT_NO_RESTART
    action = cfg.trigger_action
    if action == "submit":
        ok, why = rehearsal_ok()
        if ok:
            say(f"Auto-buy armed ({why}). Caps: " +
                ", ".join(f"{m.key} ≤ ${cfg.cap_for(m):,.2f}" for m in cfg.models))
        else:
            action = "review"
            say(f"Auto-buy NOT armed ({why}). Confirmed triggers will fill checkout; you tap Place Order.")
            notifier.send("Auto-buy not armed", f"{why}. Trigger mode will fill checkout and you tap "
                          "Place Order. Run `./duo rehearse` to re-arm.", urgent=False, tags="warning")
    act_cfg = dataclasses.replace(cfg, checkout_mode=action)
    nonces = trig.NonceStore(STATE_DIR / "trigger-nonces.json")
    drop_log = DropLog(STATE_DIR / "drops.jsonl")
    health = Health(notifier, daily_hour=cfg.daily_summary_hour, stall_minutes=cfg.stall_minutes,
                    healthcheck_url=cfg.healthcheck_url, kind="trigger")
    listen_fn = listen_fn or trig.listen
    state = {"last_reject_alert": 0.0, "last_signin_check": 0.0, "signed_out": False}

    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        n = empty_bag(page)
        if n:
            say(f"Emptied {n} item(s) from your bag so checkout only contains the phone.")
        say(f"Trigger mode: listening on the trigger topic for {', '.join(m.key for m in cfg.models)}. "
            f"Confirmed drops → {action}. Ctrl-C to stop.")
        notifier.send("Duo-tracker started", f"Trigger mode on {socket.gethostname()} "
                      f"({', '.join(m.key for m in cfg.models)}; confirmed drops → {action}).",
                      urgent=False, tags="eyes")
        try:
            events = listen_fn(cfg.trigger_server, cfg.trigger_topic, token=cfg.trigger_token,
                               backoff_s=cfg.trigger_reconnect_backoff_seconds)
            for ev in events:
                if ev.kind in ("connected", "keepalive"):
                    if ev.kind == "connected":
                        say("trigger listener connected")
                    health.alive()
                    _trigger_signin_check(page, cfg, notifier, state, signed_in)
                elif ev.kind == "disconnected":
                    health.problem(f"trigger listener: {ev.text}")
                    say(f"trigger listener disconnected ({ev.text}); reconnecting", logging.WARNING)
                elif ev.kind == "message":
                    result = _handle_trigger(ev.text, page=page, cfg=cfg, act_cfg=act_cfg, notifier=notifier,
                                             health=health, nonces=nonces, drop_log=drop_log, state=state)
                    if result is not None:
                        _hold_after_acting(health)
                        return 0
                health.tick()
        except PermissionError as e:
            say(str(e), logging.ERROR)
            notifier.send("Duo-tracker can't listen for triggers", str(e), urgent=False, tags="warning")
            return EXIT_NO_RESTART
        except KeyboardInterrupt:
            say("bye")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def _trigger_signin_check(page, cfg, notifier, state, signed_in) -> None:
    if time.monotonic() - state["last_signin_check"] < cfg.signin_check_minutes * 60:
        return
    state["last_signin_check"] = time.monotonic()
    try:
        ok = signed_in(page)
    except Exception as e:
        say(f"sign-in check failed: {e}", logging.WARNING, exc=e)
        return
    if not ok and not state["signed_out"]:
        state["signed_out"] = True
        say("Signed out of Apple — run `./duo setup` and sign in, or checkout will stop.", logging.WARNING)
        notifier.send("⚠️ Signed out of Apple", "Sign back in on the tracker's browser, or a triggered "
                      "checkout will stop at sign-in.", tags="warning")
    elif ok and state["signed_out"]:
        state["signed_out"] = False
        notifier.send("Signed back in", "Triggered checkout is ready again.", urgent=False, tags="white_check_mark")


def _handle_trigger(raw, *, page, cfg, act_cfg, notifier, health, nonces, drop_log, state) -> dict | None:
    """Validate one trigger, confirm it with Apple, and act. None = nothing was attempted."""
    from . import trigger as trig
    from .availability import matching_stores
    from .browser import Blocked, check_round

    by_part = {m.part: m for m in cfg.models}
    try:
        t = trig.validate(raw, secret=cfg.trigger_secret, parts=set(by_part), store_allowed=cfg.store_allowed,
                          max_age_seconds=cfg.trigger_max_age_seconds, nonces=nonces)
    except trig.TriggerRejected as e:
        health.trigger(False)
        say(f"REJECTED trigger: {e}. Nothing bought.", logging.WARNING)
        if time.monotonic() - state["last_reject_alert"] >= REJECT_ALERT_EVERY_S or not state["last_reject_alert"]:
            state["last_reject_alert"] = time.monotonic()
            notifier.send("⚠️ Trigger rejected", f"{e}. Nothing was bought. If your detector didn't send this, "
                          "someone knows the trigger topic: run `./duo trigger-init --rotate`.", tags="warning")
        return None

    health.trigger(True)
    m = by_part[t.part]
    say(f"Trigger: {m.name} at {t.store_number} (seen {t.age():.0f}s ago) — confirming with Apple")
    av = None
    try:
        for _m, av in check_round(page, [m], cfg.carrier, zip_code=cfg.zip, retry_budget=30):
            pass
    except Blocked as e:
        av = e
    if av is None or isinstance(av, Exception):
        why = f"Apple blocked the check ({av})" if isinstance(av, Blocked) else f"check failed: {av}"
        if isinstance(av, Blocked):
            health.blocks += 1
        say(f"Couldn't confirm trigger: {why}. Nothing bought.", logging.WARNING)
        notifier.send(f"Trigger for {m.name} — couldn't confirm", f"{why}. Nothing bought. Check the "
                      "Apple app yourself.", click=BUY_URL)
        return None

    stores = matching_stores(av, cfg.store_filter, cfg.max_miles)
    if t.store_number.lower() not in {s.store_number.lower() for s in stores}:
        others = ", ".join(s.store_name for s in stores) or "none"
        say(f"Trigger did not match stock: {m.name} not in stock at {t.store_number} "
            f"(in-stock matching stores: {others}). Nothing bought.", logging.WARNING)
        notifier.send("Trigger did not match stock", f"{m.name} at {t.store_number}: Apple says no. "
                      f"In stock nearby: {others}. Nothing bought.", urgent=False, tags="warning")
        return None

    health.drop()
    named = next(s for s in stores if s.store_number.lower() == t.store_number.lower())
    say(f"🚨 DROP (trigger confirmed): {m.name} — {', '.join(s.store_name for s in stores)}")
    notifier.send(f"🚨 {m.name} IN STOCK", f"Trigger confirmed: {', '.join(s.store_name for s in stores[:4])}. "
                  f"${m.price}", click=BUY_URL)
    drop_id = drop_log.start(key=f"pickup:{named.store_number}:{m.part}", model=m.name, part=m.part,
                             store=named.store_name, store_number=named.store_number, kind="pickup",
                             notice_gap_s=t.age(), window="trigger")
    result = _act_on_hit(page, act_cfg, notifier, m, stores)
    drop_log.outcome(drop_id, result["outcome"], result.get("detail", ""), result.get("checkout_s"))
    return result


def cmd_trigger_init(args) -> int:
    """Generate a private inbound topic + HMAC secret and write them into config.yaml."""
    import re
    import secrets as _secrets
    from pathlib import Path

    path = Path(args.config)
    text = path.read_text()
    topic = "duo-trigger-" + _secrets.token_hex(12)
    secret = _secrets.token_hex(32)
    block = re.search(r"(?m)^trigger:[^\n]*\n((?:[ \t]+[^\n]*\n?|[ \t]*\n)*)", text)
    if block and not args.rotate:
        print("config already has a `trigger:` section. Use --rotate to replace its topic and secret.")
        return 1
    if block:
        body = block.group(1)
        new = re.sub(r"(?m)^(\s+topic:).*$", lambda mm: f'{mm.group(1)} "{topic}"', body)
        new = re.sub(r"(?m)^(\s+secret:).*$", lambda mm: f'{mm.group(1)} "{secret}"', new)
        if new == body:
            print("couldn't find topic:/secret: lines under `trigger:`; edit config.yaml by hand.")
            return 1
        text = text[:block.start(1)] + new + text[block.end(1):]
    else:
        text = text.rstrip("\n") + (
            "\n\n# Trigger mode (TRIGGER_MODE.md). Private: anyone with topic+secret can wake the bot.\n"
            "trigger:\n"
            "  server: https://ntfy.sh\n"
            f'  topic: "{topic}"     # inbound only; never the ntfy alert topic\n'
            f'  secret: "{secret}"\n'
            '  token: ""             # optional ntfy access token (reserved topic)\n'
            "  action: submit        # what a confirmed trigger does: submit | review\n"
            "  max_age_seconds: 90\n"
            "  reconnect_backoff_seconds: 5\n")
    path.write_text(text)
    path.chmod(0o600)
    print(f"Wrote a new trigger topic and secret to {path} (file is now private to you).")
    print("Give both to your detector; it must sign payloads as in TRIGGER_MODE.md.")
    print("Then set `checkout: {mode: trigger}` and run ./duo watch.")
    return 0


def cmd_trigger_send(args) -> int:
    """Publish one signed trigger — to test the listener end to end, or as a manual detector."""
    from datetime import datetime, timedelta, timezone

    from . import trigger as trig
    from .catalog import resolve
    from .config import load

    cfg = load(args.config)
    if not (cfg.trigger_topic and cfg.trigger_secret):
        print("No trigger topic/secret in config. Run `./duo trigger-init` first.")
        return 1
    model = resolve(args.model) if args.model else cfg.models[0]
    seen = datetime.now(timezone.utc).astimezone() - timedelta(seconds=args.age)
    payload = trig.make(model.part, args.store.upper(), cfg.trigger_secret, seen_at=seen)
    if args.bad_sig:
        payload["sig"] = "0" * 64
    print(f"Trigger: {model.name} at {args.store.upper()}, seen_at {payload['seen_at']}"
          + (" (deliberately bad signature)" if args.bad_sig else ""))
    if not args.yes:
        print("A listening bot will check Apple and, if that store really has it, run checkout "
              f"(action: {cfg.trigger_action}). Re-run with --yes to send.")
        return 1
    trig.publish(cfg.trigger_server, cfg.trigger_topic, payload, token=cfg.trigger_token)
    print("sent")
    return 0


def cmd_rehearse(args) -> int:
    """Walk the full checkout without placing anything; arms `submit` mode on success."""
    from playwright.sync_api import sync_playwright

    from .browser import go_to_bag, load_buy_page, open_context, select_config, _page
    from .checkout import CheckoutStopped, Plan, empty_bag, format_timings, record_rehearsal, run, signed_in
    from .config import load

    cfg = load(args.config)
    model = cfg.models[0]
    results = []
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = _page(ctx)
        try:
            if not signed_in(page):
                raise CheckoutStopped("sign-in", "not signed in to Apple — run `setup` first")
            say("    ✔ signed in to Apple")
            empty_bag(page)
            say(f"1/2 delivery rehearsal with {model.name} (stops on the review page, never orders)…")
            load_buy_page(page)
            select_config(page, model, cfg.carrier)
            if not go_to_bag(page):
                raise CheckoutStopped("buy page", "couldn't click Add to Bag")
            r = run(page, Plan(model=model, fulfillment="delivery", max_total=cfg.cap_for(model)))
            results.append(f"delivery ok, review total ${r.total:,.2f}")
            say(f"    ✔ reached review — total ${r.total:,.2f} (cap ${cfg.cap_for(model):,.2f})")
            say(f"      timings: {format_timings(r.timings)}")

            if args.pickup_item_url:
                say("2/2 pickup rehearsal with the accessory you gave (stops on review)…")
                empty_bag(page)
                page.goto(args.pickup_item_url, wait_until="domcontentloaded", timeout=60_000)
                if not go_to_bag(page):
                    raise CheckoutStopped("accessory page", "couldn't click Add to Bag")
                r = run(page, Plan(model=model, fulfillment="pickup", check_model_name=False))
                results.append(f"pickup ok at {r.store or 'first store'}")
                say(f"    ✔ pickup flow reached review at {r.store or 'the first store'} — total ${r.total:,.2f}")
                say(f"      timings: {format_timings(r.timings)}")
            else:
                say("2/2 skipped pickup rehearsal (pass --pickup-item-url with any in-stock accessory to test it)")
        except CheckoutStopped as e:
            record_rehearsal(False, str(e))
            say(f"    ✘ stopped at {e.step}: {e.reason}", logging.WARNING)
            if e.timings:
                say(f"      timings: {format_timings(e.timings)}")
            if e.report:
                say(f"      Send this file to fix the selectors: {e.report}")
            return 2
        finally:
            try:
                empty_bag(page)
            except Exception:
                pass
            ctx.close()
    record_rehearsal(True, "; ".join(results))
    say("Rehearsal passed — `checkout.mode: submit` is armed for 7 days.")
    return 0


def cmd_blocks(_args) -> int:
    from .checkout import STATE_DIR
    from .schedule import summarize_blocks
    print(summarize_blocks(STATE_DIR / "blocks.csv"))
    return 0


def cmd_drops(args) -> int:
    from pathlib import Path

    from .checkout import STATE_DIR
    from .drops import export_csv, summarize
    src = STATE_DIR / "drops.jsonl"
    if args.csv:
        n = export_csv(src, Path(args.csv))
        print(f"Wrote {n} drops to {args.csv}")
        return 0
    print(summarize(src, days=args.days))
    return 0


def cmd_report(args) -> int:
    from pathlib import Path

    from .checkout import STATE_DIR
    from .report import build
    out = build(STATE_DIR, Path(args.config))
    print(f"Wrote {out}\nEmails, phone numbers, addresses, card numbers, order numbers and your ntfy topic "
          "are removed. Skim it, then send it over.")
    return 0


def _notify_crash(config_path: str, e: BaseException) -> None:
    try:
        from .config import load
        cfg = load(config_path)
        Notifier(cfg.ntfy_server, cfg.ntfy_topic).send(
            "💥 Duo-tracker crashed", f"{type(e).__name__}: {str(e)[:160]}. "
            "The service restarts it in 30s; `./duo report` has the details.", urgent=False, tags="boom")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="duo_tracker", description="Watch Apple for iPhone Duo pickup stock.")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="interactive setup: writes config.yaml").set_defaults(fn=cmd_init)
    sub.add_parser("models", help="list model keys and part numbers").set_defaults(fn=cmd_models)
    sub.add_parser("setup", help="sign in and choose a store in the bot's browser profile").set_defaults(fn=cmd_setup)
    sub.add_parser("notify-test", help="send a test push, and ping the healthcheck if set").set_defaults(fn=cmd_notify_test)
    sub.add_parser("check", help="check each model once and print results").set_defaults(fn=cmd_check)
    sub.add_parser("watch", help="keep checking; act on stock per checkout.mode").set_defaults(fn=cmd_watch)
    ti = sub.add_parser("trigger-init", help="create a private trigger topic + signing secret in config.yaml")
    ti.add_argument("--rotate", action="store_true", help="replace an existing topic and secret")
    ti.set_defaults(fn=cmd_trigger_init)
    ts = sub.add_parser("trigger-send", help="publish one signed trigger (testing / manual detector)")
    ts.add_argument("--store", required=True, help="store number, e.g. R068")
    ts.add_argument("--model", help="model key (default: first in config)")
    ts.add_argument("--age", type=int, default=0, help="backdate seen_at by N seconds (test expiry)")
    ts.add_argument("--bad-sig", action="store_true", help="send a wrongly signed trigger (test rejection)")
    ts.add_argument("--yes", action="store_true", help="actually send it")
    ts.set_defaults(fn=cmd_trigger_send)
    sub.add_parser("blocks", help="summarize when Apple blocked checks, to tune the schedule").set_defaults(fn=cmd_blocks)
    d = sub.add_parser("drops", help="every drop seen and what the bot did about it")
    d.add_argument("--days", type=int, default=7)
    d.add_argument("--csv", metavar="PATH", help="export the full history as CSV instead")
    d.set_defaults(fn=cmd_drops)
    sub.add_parser("report", help="bundle logs + diagnostics into one scrubbed file to send for help"
                   ).set_defaults(fn=cmd_report)
    r = sub.add_parser("rehearse", help="walk checkout to the review page without ordering (arms submit mode)")
    r.add_argument("--pickup-item-url", help="apple.com product page of any in-stock accessory, to test the pickup steps")
    r.set_defaults(fn=cmd_rehearse)
    args = p.parse_args(argv)
    from . import checkout
    from .logsetup import setup
    setup(checkout.STATE_DIR if args.cmd not in ("models", "init") else None)
    log.debug("command: %s", " ".join(sys.argv[1:] if argv is None else argv))
    try:
        return args.fn(args)
    except Exception as e:
        from .browser import NoScreenError
        if not isinstance(e, (ValueError, KeyError, FileNotFoundError, NoScreenError)):
            log.critical("crashed: %s: %s", type(e).__name__, e, exc_info=True)
            if args.cmd == "watch":
                _notify_crash(args.config, e)
            return 1
        log.error("error: %s", e)
        log.debug("details", exc_info=True)
        return EXIT_NO_RESTART
    except KeyboardInterrupt:
        return 0
