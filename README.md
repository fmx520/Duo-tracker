# Duo-tracker

Watches apple.com for **iPhone Duo in-store pickup stock** near you. The moment
a model you want shows up at a store, it pushes an urgent alert to your phone
and, in `submit` mode, **buys it for you**: it adds the phone to your bag,
chooses pickup at that store, picks the earliest pickup time, pays with your
saved card, and places the order. It stops and hands you the window the moment
anything looks off.

> **Status, 2026-11-03:** the Mac stopgap install is complete, but Apple is
> returning HTTP 541 to availability requests and `./duo setup` has not
> completed. Before working on this, read **[HANDOFF.md](HANDOFF.md)** — it
> records what is already verified so you don't redo it. AI agents: see
> **[AGENTS.md](AGENTS.md)**.

## How it works (and what it deliberately doesn't do)

- It drives **a real, visible Chrome window** on your Mac, using its own
  profile that you sign in to once. Every round it loads the Duo buy page,
  selects your model, and reads the availability answer **the page itself
  fetched** from Apple. It never calls Apple's APIs directly.
- It checks **faster when drops actually happen and slower otherwise.** The
  default is every 30s from 7:30–9:30am ET and 11:30pm–12:30am ET, and every
  90s the rest of the day (30s is the floor). Each round loads the page
  **once** and switches between your models on it, and each switch makes the
  page ask Apple about that model.
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

## Install

The repo is private, so log in to GitHub first. On Ubuntu, `gh` comes from snap
(it isn't in apt on 22.04):

```bash
sudo apt update && sudo apt install -y git
sudo snap install gh            # macOS: brew install gh
gh auth login                   # GitHub.com → HTTPS → yes, authenticate Git → login with a web browser
gh repo clone fmx520/Duo-tracker && cd Duo-tracker && ./install.sh
```

`install.sh` sets up Python packages and a browser, and on Linux also the
virtual screen and VNC server. It then runs the **setup wizard** (`./duo init`).
The wizard asks which models, your carrier, the mode, the distance, and your
ntfy topic, writes `config.yaml`, and sends a test notification. Install the
**ntfy** app on your phone and subscribe to the topic it shows you.

Everything after that goes through `./duo`:

| Command | What it does |
|---|---|
| `./duo setup` | Opens the bot's browser. Sign in to your Apple Account (approve 2FA), choose any Duo config, click **Check availability**, enter your ZIP and pick a store, then close the window. |
| `./duo check` | Checks stock once (`0/12 stores have it`). "No store data" means redo `setup`. |
| `./duo rehearse` | Practice checkout (see below). Never orders. A pass arms auto-buy for 7 days. |
| `./duo watch` | Watches in this terminal. |

## Running 24/7 on a headless Linux box (recommended)

A spare always-on computer at home is the best place for this. It's on your
home internet and in your real browser profile, and it doesn't compete with
you for a screen. Cloud servers don't work: Apple blocks data-center
connections.

```bash
./install.sh                         # once
scripts/install-linux-service.sh     # once: virtual screen + VNC + auto-start
./duo setup                          # then VNC in (below) and sign in to Apple
./duo rehearse
./duo start
```

- The bot draws its Chrome window on a **virtual screen**. To see or drive it,
  connect a VNC viewer to `vnc://<box-ip>:5900` (`./duo vnc` prints the address)
  with the password you chose. On a Mac: Finder → Go → Connect to Server. On an
  iPhone: a VNC app such as RealVNC Viewer. This is how you sign in, and how you
  take over when checkout says **"needs you"**.
- VNC is for your home network only. **Don't port-forward 5900.** To reach it
  away from home, put the box and your phone on [Tailscale](https://tailscale.com).
- `./duo status` shows whether things are healthy, `./duo logs` follows the
  output, and `./duo stop` / `./duo start` control the watcher. It restarts
  itself after crashes and reboots. `setup`, `check` and `rehearse` pause the
  background watcher while they run and resume it afterwards.
- The service installer offers to stop the box from ever sleeping. Say yes.

## Running on a Mac

`./install.sh`, then `./duo setup` and `./duo rehearse`, then **double-click
`Start Duo Tracker.command`**. It keeps the Mac awake while it watches. It only
watches while the Mac is on and that window is open.

## Rehearse checkout before it buys anything

`./duo rehearse` first confirms you're signed in to Apple. It then does the
whole checkout with a **delivery** order (Duo delivery is still orderable,
shipping in January), **stops on the review page without ordering**, and
empties your bag. It prints how long each step took.

Add `--pickup-item-url <any in-stock accessory page>` to also rehearse the
pickup steps (choosing a store and a pickup time). Please do. If a step misses,
it saves a small JSON report of that page's buttons (labels only, no personal
field values) under `~/.duo-tracker/reports/`. Send it over and the selectors
get fixed.

## Staying informed: health alerts

The scary failure is a watcher that quietly stopped working. So it tells you:

| Alert | When |
|---|---|
| **Duo-tracker started** | Every start, so you also hear about reboots and restarts |
| **Daily check-in** | Once a day (9am by default): healthy or stalled, plus checks and drops since yesterday |
| **⚠️ stalled** / **recovered** | No successful check for 10 min, then again when checks come back |
| **⚠️ Signed out of Apple** | Hourly sign-in check failed (review/submit modes), so checkout would hit a sign-in page. VNC in and sign in. |
| **paused** | Apple blocked checks. It backs off and says so. |

**If the whole box dies** (power cut, internet down), it can't tell you
itself. For that, create a free check at [healthchecks.io](https://healthchecks.io).
Set its period to 5 minutes, connect it to your phone (it supports ntfy, email,
and more), and paste its ping URL into `health.healthcheck_url`. The tracker
pings it after successful checks, and healthchecks.io alerts you when the pings
stop.

When stock hits you'll get **🚨 iPhone Duo … IN STOCK**, then one of:

- **✅ iPhone Duo ORDER PLACED**: model, store, total, and order number. Done.
  Apple emails the confirmation, and the bot stops.
- **⚠️ Checkout needs you NOW**: it hit something it won't do by itself (see
  below). The window is open at that exact step, so finish it there.

In `alert` mode it keeps watching and pings you for each new drop. In every
other mode it stops after acting so it doesn't fight you for the page.

## Logs, drop history, and getting help

| Command | Shows |
|---|---|
| `./duo logs` | Follows the log live. Everything lands in `~/.duo-tracker/logs/duo-tracker.log` with full error details, rotated daily, 7 days kept. Same on Linux and Mac. |
| `./duo drops` | Every drop seen in the last 7 days (`--days 30` for more): when, store, model, how quickly it was noticed, how long it stayed, and the outcome (**ordered**, **review ready**, **needs you**, **bagged**, **add-to-bag failed**, or **alert only**). `--csv drops.csv` exports it all. |
| `./duo blocks` | When Apple blocked checks, and at what rate. |
| `./duo report` | Bundles config, practice-run results, blocks, drops, the last 3 checkout reports and the recent log into one file in `~/.duo-tracker/support/`. Emails, phone numbers, street addresses, card numbers, order numbers, your ntfy topic and your healthcheck URL are scrubbed. Skim it, then send it for help. |

If the watcher crashes you get **💥 Duo-tracker crashed: <error>** on your
phone. The Linux service restarts it 30 seconds later, and you'll then see
"started" again. An unexpected error *during checkout* doesn't crash it: you
get **⚠️ Checkout needs you NOW** with the window left open.

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
- **It won't arm without a recent passing `rehearse`** (within 7 days). Without one it
  keeps watching in `review` mode (fills checkout, you tap Place Order) and tells you to re-run it.
- **It empties your bag when `watch` starts**, so checkout contains only the phone.
- **It picks the nearest in-stock store** that passes your `stores`/`max_miles`
  filters. Courier-only drops get an alert but aren't auto-bought.

Before running `submit`, make sure your Apple Account has a saved card with a
billing address, and that the contact details for pickup are filled in.

**Heads-up:** Apple can cancel orders it decides were placed by a bot. One
phone, bought from your own signed-in browser without disguising it, is the
lowest-risk version of this, but not risk-free.

## Tuned for 11754 (Kings Park, NY)

The defaults (`./duo init` with ZIP 11754, and `config.example.yaml`) come
from the public drop history of the NYC-area tracker for Oct 26 – Nov 2:

| Store (straight-line distance) | Drops | Median length |
|---|---|---|
| Smith Haven, Lake Grove (6.7 mi) | not tracked there | – |
| Walt Whitman, Huntington Station (9.2 mi) | 7 | 73s |
| Roosevelt Field, Garden City (21.3 mi) | 15 | 65s |
| Manhasset (22.9 mi) | 22 | 30s |

- Long Island drops last **about a minute**, versus about 25s in the city.
  Nearly all of them landed **7:30–9:30am ET**.
- Almost all drops were **256GB and 512GB**, so those four models are the default.
- Norwalk and Greenwich look close on a map but are across the Sound, so the
  store list is filtered by name (and Apple store number), not just by miles.

**The schedule adapts.** If Apple blocks a check during a fast window, the
rest of that window drops back to the slow rate. Every block is logged.
`./duo blocks` shows how often blocks happened at each rate, and the daily
check-in includes a block count. After a week, tune `schedule:` to what your
connection actually tolerates.

## Config

See [`config.example.yaml`](config.example.yaml). The settings most people change:

| key | meaning |
|-----|---------|
| `models` | keys from `models`, e.g. `256gb-nightsky`, or part numbers |
| `carrier` | `unlocked` / `att` / `tmobile` / `verizon` |
| `zip` | your ZIP; `setup` reminds you to enter it in the bot's browser |
| `stores` | optional; only alert for stores whose name/city/number matches |
| `schedule` | `default_seconds`, plus `windows` of `{start, end, seconds}` in `timezone` (min 30s) |
| `max_miles` | optional; ignore stores farther than this |
| `alert_on_courier` | also alert when same-day courier delivery is available |
| `checkout.mode` | `alert` (push only), `bag` (default), `review` (fill checkout, you click Place Order), or `submit` (full auto) |
| `checkout.max_total` | optional hard cap on the order total; default is model price × 1.11 |
| `health.*` | `daily_summary_hour` (or `null`), `stall_minutes`, `healthcheck_url` |
| `signin_check_minutes` | how often to confirm you're still signed in to Apple (default 60) |

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
