# Duo-tracker

Watches apple.com for **iPhone Duo in-store pickup stock** near you. The moment
a model you want shows up at a store, it pushes an urgent alert to your phone
and, in `submit` mode, **buys it for you**: it adds the phone to your bag,
chooses pickup at that store, picks the earliest pickup time, pays with your
saved card, and places the order. It stops and hands you the window the moment
anything looks off.

## How it works (and what it deliberately doesn't do)

- It drives **a real, visible Chrome window** on your Mac, using its own
  profile that you sign in to once. Every round it loads the Duo buy page,
  selects your model, and reads the availability answer **the page itself
  fetched** from Apple. It never calls Apple's APIs directly.
- It checks at a fixed, polite interval (30s default and minimum). Each round
  loads the page **once** and switches between your models on it, and each
  switch makes the page ask Apple about that model. Drops tend to last only
  about 2 minutes (per [duo-tracker-six](https://duo-tracker-six.vercel.app/)),
  so speed matters.
- It alerts only on **new** drops: a (store, model) pair, or same-day courier
  delivery, that wasn't in stock last round. A model sitting in stock doesn't
  spam you, and a re-drop alerts again.
- **If Apple refuses** (HTTP 541/403/429 or a challenge page), it stops,
  pushes you a "paused" note, and waits 10 minutes, doubling up to an hour.
  It has no stealth tricks, no fingerprint spoofing, and no CAPTCHA solving.
- **Automated checkout runs inside strict limits** (see below). It never types
  passwords, 2FA codes, or card numbers.

Apple doesn't want automated stock checking, and this tool respects that. If
Apple blocks it for a while, it waits instead of trying to sneak around the
block.

## Setup (Mac, ~5 minutes)

```bash
git clone https://github.com/fmx520/Duo-tracker.git && cd Duo-tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # fallback if you don't have Google Chrome

cp config.example.yaml config.yaml # then edit: models, carrier, ntfy topic
python -m duo_tracker models       # list model keys / part numbers / prices
```

1. **Phone alerts:** install the **ntfy** app (iOS/Android) and subscribe to a
   long, unguessable topic name. Put the same name in `config.yaml` under
   `ntfy.topic`. Then run `python -m duo_tracker notify-test`.
2. **Sign in and pick a store:** run `python -m duo_tracker setup`. A Chrome window
   opens. Sign in to your Apple Account (approve 2FA), choose any Duo config,
   click **Check availability**, enter your ZIP, and select a store. Then close
   the window. Apple remembers the store in this profile, and the store list it
   returns is what the bot watches.
3. **Dry run:** run `python -m duo_tracker check`. It checks each model once and
   prints something like `0/12 stores have it`. If it says "no store data", redo step 2.
4. **Rehearse checkout:** run `python -m duo_tracker rehearse`. It does the
   whole checkout with a **delivery** order (Duo delivery is still orderable,
   shipping in January), **stops on the review page without ordering**, then
   empties your bag. Add `--pickup-item-url <any in-stock accessory page>` to
   also rehearse the pickup steps, including choosing a store and a pickup
   time. Do this one if you can. If a step misses, it saves a small JSON
   report of that page's buttons (labels only, no personal field values).
   Send it over and the selectors get fixed. A passing rehearsal arms
   `submit` mode for 7 days.
5. **Go:** run `python -m duo_tracker watch`. Leave the window alone, but you
   can minimize it. Keep the Mac awake with `caffeinate -dims python -m duo_tracker watch`.

When stock hits you'll get **🚨 iPhone Duo … IN STOCK**, then one of:

- **✅ iPhone Duo ORDER PLACED**: model, store, total, and order number. Done.
  Apple emails the confirmation, and the bot stops.
- **⚠️ Checkout needs you NOW**: it hit something it won't do by itself (see
  below). The window is open at that exact step, so finish it there.

In `alert` mode it keeps watching and pings you for each new drop. In every
other mode it stops after acting so it doesn't fight you for the page.

## Automated checkout: the limits

`checkout.mode: submit` places a real order. It is built to fail safe:

| It stops and pings you when… | Why |
|---|---|
| Apple asks you to sign in again or approve 2FA | It never handles your credentials |
| Apple asks for the card security code, or there's no saved card | No card data is stored anywhere |
| The review page doesn't show the expected model, or the store it chose | It never buys the wrong thing |
| The total is missing or above the cap (price × 1.11 by default, or `checkout.max_total`) | This also catches two phones in the bag |
| A page or button isn't what it expects (layout change, challenge) | It never guesses |

On top of that:
- **One order, ever.** Right before clicking Place Order it writes
  `~/.duo-tracker/order.lock`. While that file exists it refuses to buy again,
  even after a crash. Delete the file to allow another order.
- **It won't arm without a recent passing `rehearse`** (within 7 days).
- **It empties your bag when `watch` starts**, so checkout contains only the phone.
- **It picks the nearest in-stock store** that passes your `stores`/`max_miles`
  filters. Courier-only drops get an alert but aren't auto-bought.

Before running `submit`, make sure your Apple Account has a saved card with a
billing address, and that the contact details for pickup are filled in.

**Heads-up:** Apple can cancel orders it decides were placed by a bot. One
phone, bought from your own signed-in browser without disguising it, is the
lowest-risk version of this, but not risk-free.

## Config

See [`config.example.yaml`](config.example.yaml). The settings most people change:

| key | meaning |
|-----|---------|
| `models` | keys from `models`, e.g. `256gb-nightsky`, or part numbers |
| `carrier` | `unlocked` / `att` / `tmobile` / `verizon` |
| `stores` | optional; only alert for stores whose name/city/number matches |
| `max_miles` | optional; ignore stores farther than this |
| `alert_on_courier` | also alert when same-day courier delivery is available |
| `checkout.mode` | `alert` (push only), `bag` (default), `review` (fill checkout, you click Place Order), or `submit` (full auto) |
| `checkout.max_total` | optional hard cap on the order total; default is model price × 1.11 |

## Prior art

- [kelindi/apple-stock](https://github.com/kelindi/apple-stock) (MIT). The
  response-field names (`stickyMessageIDL`/`idl` for courier,
  `storePickupQuote` fallback, distance units) and the "alert only on new keys"
  idea come from its parser. **Deliberately not copied:** its user-agent
  spoofing, removal of Chrome's automation flags, and retry-until-the-challenge-
  clears loop. Those exist to get around Apple's bot shield, and this project
  won't do that.
- [duo-tracker-six.vercel.app](https://duo-tracker-six.vercel.app/): a hosted
  NYC-area pickup radar with push alerts. If you're in NYC it's worth
  subscribing to alongside this.

## Development

```bash
pip install pytest
pytest -q
```

`tests/test_browser_flow.py` runs the real browser code against a fake
apple.com that it answers itself, so the tests never touch Apple. The page
hooks it mirrors (`data-autom` radio inputs, `add-to-cart`, and the
`/shop/fulfillment-messages` fetch) come from the live buy page in Nov 2026.

`tests/test_checkout.py` drives the full checkout against a fake Apple
checkout. It covers placing exactly one order, every stop condition, the
one-order lock, and the watcher's hit handler.

**Not verified against the live site:** Apple's real checkout pages (they need
a signed-in bag) and the exact shape of a *positive* pickup response. Nothing
was in stock to observe. `rehearse` exists to close the first gap before any
real money is involved. Both fail safe: if a click misses, the
bot leaves the page open, tells you, and you take over. If Apple changes the
page, `check` will say "the page never asked Apple for availability".
