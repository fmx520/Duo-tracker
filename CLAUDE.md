# Duo-tracker — notes for Claude Code sessions

Watches apple.com for iPhone Duo in-store pickup stock near ZIP 11754
(Kings Park, NY) and, in `submit` mode, buys one automatically. README.md has
setup and usage; this file has the decisions and rules behind it.

> **Current status (2026-11-03):** the Mac stopgap is installed, but Apple is
> returning HTTP 541 to availability requests and `setup` never completed.
> Read [HANDOFF.md](HANDOFF.md) before picking this up — it records what is
> already verified so you don't redo it.

## Hard rules (decided with the owner — don't relitigate, don't work around)

- **No bot-detection evasion.** No user-agent spoofing, no removing Chrome
  automation flags / `navigator.webdriver`, no stealth plugins, fingerprint or
  header randomisation, decoy requests, CAPTCHA solving, residential proxies,
  or retry-until-the-challenge-clears loops. kelindi/apple-stock and its
  Lanbig fork do several of these; we deliberately took only their response
  parsing. If Apple blocks (541/403/429), the bot backs off and says so.
- **Never call Apple's `/shop/fulfillment-messages` ourselves.** The bot drives
  a real, visible Chrome on the buy page and reads the response *the page*
  fetched (`browser.check_model`). Cloud/data-centre IPs get 541 regardless.
- **Checkout safety stops stay.** Stop (never automate) on Apple sign-in/2FA,
  card security code or card entry, wrong model/store on review, total over
  cap, unknown page. One order ever per machine (`~/.duo-tracker/order.lock`,
  written *before* clicking Place Order). Submit needs a passing `rehearse`
  within 7 days, otherwise it watches in `review` mode.
- **Never run two watchers at once** (e.g. the Mac and the Linux box): same
  home IP doubles Apple's view of us, and each machine has its own order lock,
  so two `submit` watchers could buy two phones.

## Where things run

- Target: an always-on Ubuntu box (OptiPlex, being upgraded 20.04 → 22.04;
  needs Python 3.10+, Playwright doesn't support 20.04) via
  `scripts/install-linux-service.sh` (Xvfb + x11vnc + systemd user units).
- Stopgap: the owner's Mac (`./install.sh`, then `Start Duo Tracker.command`).
  macOS's built-in python3 may be 3.9 — `install.sh` picks the newest 3.10+.
- Things only the owner can do: Apple sign-in + 2FA in the bot's browser
  (`./duo setup`), sudo/admin passwords, `gh auth login`.

## Working on it

- Tests: `pytest -q` (≈80 tests, ~90s; browser tests run against fake
  apple.com pages via request routing — nothing touches Apple). On Linux CI
  boxes set `PLAYWRIGHT_BROWSERS_PATH` if browsers live elsewhere.
- Never let tests reach the network: fakes must not answer with HTTP 3xx
  redirects (Playwright doesn't route the follow-up request).
- Real-site selectors for checkout are unverified until `./duo rehearse`
  passes on a signed-in profile; failures write
  `~/.duo-tracker/reports/*.json` — fix selectors in `duo_tracker/checkout.py`
  from those.
- Diagnostics: `./duo logs`, `./duo drops`, `./duo blocks`, `./duo report`
  (scrubbed bundle). State lives in `~/.duo-tracker/`.

## Data behind the defaults (public drop history, Oct 26 – Nov 2 2026)

Near 11754: Walt Whitman R068 (7 drops, median 73s), Roosevelt Field R060
(15, 65s), Manhasset R327 (22, 30s); Smith Haven R139 untracked. ~2/3 of drops
land 7:30–9:30am ET, some around midnight ET; almost all 256GB/512GB. Hence
the schedule (30s at peak, 90s otherwise, fallback after a block).
