# Duo-tracker

Watches apple.com for **iPhone Duo in-store pickup stock** near you. The moment
a model you want shows up at a store, it pushes an urgent alert to your phone,
adds the phone to your bag, and opens checkout. You pick the store and pay.
That takes about 20 seconds with Apple Pay.

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
- **It never places an order.** At most it adds to bag and clicks Check Out.
  The final tap is yours.

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
4. **Go:** run `python -m duo_tracker watch`. Leave the window alone, but you
   can minimize it. Keep the Mac awake with `caffeinate -dims python -m duo_tracker watch`.

When stock hits, you'll get **🚨 iPhone Duo … IN STOCK** followed by **Finish
your iPhone Duo order NOW**. Go to the Mac, or tap the alert to open the bag on
your phone. Choose **pickup** at the listed store and pay. In `bag`/`checkout`
mode the bot stops watching after a hit so it doesn't fight you for the page.
In `alert` mode it keeps watching and pings you for each new drop.

## Config

See [`config.example.yaml`](config.example.yaml). The settings most people change:

| key | meaning |
|-----|---------|
| `models` | keys from `models`, e.g. `256gb-nightsky`, or part numbers |
| `carrier` | `unlocked` / `att` / `tmobile` / `verizon` |
| `stores` | optional; only alert for stores whose name/city/number matches |
| `max_miles` | optional; ignore stores farther than this |
| `alert_on_courier` | also alert when same-day courier delivery is available |
| `checkout.mode` | `alert` (push only), `bag` (default), or `checkout` |

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

**Not verified against the live site:** the Check Out button selector (it
needs a signed-in bag) and the exact shape of a *positive* pickup response.
None of that was in stock to observe. Both fail safe: if a click misses, the
bot leaves the page open, tells you, and you take over. If Apple changes the
page, `check` will say "the page never asked Apple for availability".
