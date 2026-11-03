# AGENTS.md

Instructions for any AI coding agent working in this repository, whatever
tool you are. `CLAUDE.md` carries the same rules with more background; if you
only read one other file, make it `HANDOFF.md`.

## Read these first, in this order

1. **[HANDOFF.md](HANDOFF.md)** — current status. As of 2026-11-03 the project
   is blocked: Apple returns **HTTP 541** to availability requests, so
   `./duo setup` has never completed and no check has ever succeeded.
   HANDOFF.md records what is already proven, what is ruled out with evidence,
   and what is genuinely still unknown. **Do not re-derive it.**
2. [README.md](README.md) — what the tool does and how to run it.
3. [CLAUDE.md](CLAUDE.md) — the reasoning behind the design decisions.

## Hard rules — do not work around these

These are the owner's standing decisions for this project, not suggestions.

- **No bot-detection evasion.** No user-agent spoofing. No removing Chrome's
  automation flags or `navigator.webdriver`. No stealth plugins, fingerprint
  or header randomisation, or decoy requests. No CAPTCHA solving. No
  residential proxies, and no switching networks to shed a block. No
  retry-until-the-challenge-clears loops. When Apple refuses
  (541 / 403 / 429 / a challenge page), the bot **backs off and says so**.
- **Never call Apple's `/shop/fulfillment-messages` endpoint directly.** The
  bot drives a real, visible Chrome on the buy page and reads the response
  *the page itself* fetched (`browser.check_model`).
- **Checkout safety stops stay.** Stop and hand control to the owner on Apple
  sign-in / 2FA, card security code or card entry, wrong model or store on the
  review page, a total over the cap, or any unrecognised page. One order ever
  per machine, enforced by `~/.duo-tracker/order.lock`, written *before*
  clicking Place Order. `submit` mode requires a passing `rehearse` within
  7 days, otherwise it watches in `review` mode.
- **Never run two watchers at once.** The Mac and the Linux box share one home
  IP, and each machine has its own order lock — two `submit` watchers could
  buy two phones.
- **Never enter the owner's credentials.** Apple sign-in, 2FA, card numbers and
  sudo passwords are theirs to type. The tool is built never to need them.

If you believe a rule is blocking a correct fix, say so and stop. Do not route
around it quietly.

## Things already verified — don't redo them

Checked 2026-11-03 against the live Apple page. Details and method are in
HANDOFF.md.

- All 8 part numbers in `duo_tracker/catalog.py`, and the part → capacity/colour
  mapping, match Apple's own embedded product data exactly.
- Every `data-autom` selector `select_config()` clicks exists on the live page.
- The per-config `seoUrlToken` URLs resolve correctly; a "Page Not Found" seen
  during setup was **not** a bad URL.
- The install (Homebrew, Python 3.12.16, `.venv`, Playwright, Chrome) is
  complete and working on the owner's Mac.

The 404 symptom looks exactly like a stale-selector bug and is not one. If you
think you have found a catalog or selector error, re-read HANDOFF.md and say
so before changing anything.

## Working on it

- Tests: `pytest -q` (~80 tests, ~90s). Browser tests run against fake
  apple.com pages via request routing — nothing touches Apple.
- Never let tests reach the network. Fakes must not answer with HTTP 3xx
  redirects; Playwright does not route the follow-up request.
- State lives in `~/.duo-tracker/`. Diagnostics: `./duo logs`, `./duo drops`,
  `./duo blocks`, `./duo report` (scrubbed bundle, safe to share).
- `config.yaml` is gitignored and contains the owner's ntfy topic. Never copy
  that topic into a tracked file — `./duo report` scrubs it deliberately.
