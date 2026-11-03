# Handoff — Mac stopgap install, 2026-11-03

State of the Mac stopgap attempt, written for the next Claude Code session.
Read `CLAUDE.md` first (its hard rules still apply in full), then this.

**One-line summary:** the Mac is fully installed and the code is verified
correct against the live site, but Apple returned **HTTP 541 to the first
availability request this machine ever made**, so `setup` could not be
completed and nothing has ever been checked successfully.

## Don't redo these — they are confirmed working

| Thing | State |
|---|---|
| Homebrew | installed at `/opt/homebrew` (was absent; needed the owner's admin password) |
| Python | 3.12.16 via `brew install python@3.12`; macOS system python is 3.9.6 and unusable |
| `.venv` | created, `playwright` / `PyYAML` / `requests` import fine |
| Chrome | `/Applications/Google Chrome.app` present, so `install.sh` skipped the Chromium download |
| `gh` | already authenticated as `fmx520`, `repo` scope — no `gh auth login` needed |
| `config.yaml` | written by the wizard at 11754 defaults: 4 models (256/512 × nightsky/starwhite), `carrier: unlocked`, `max_miles: 30`, the 8-entry LI store filter, `checkout.mode: submit` |
| ntfy | topic generated, test push sent and `sent` confirmed. Topic is in `config.yaml` (gitignored) — don't copy it into tracked files; `duo report` scrubs it on purpose |

## Verified against the live site — catalog and selectors are correct

Checked on 2026-11-03 by reading `window.PRODUCT_SELECTION_BOOTSTRAP`
on `https://www.apple.com/shop/buy-iphone/iphone-duo`:

- All 8 part numbers in `catalog.py` match Apple's data exactly, and so does
  the part → capacity/colour mapping (`MK1F4`=256 nightsky, `MK1E4`=256 starwhite,
  `MK1H4`=512 nightsky, `MK1G4`=512 starwhite, `MK1L4`=1TB nightsky,
  `MK1J4`=1TB starwhite, `MK1R4`=2TB nightsky, `MK1Q4`=2TB starwhite).
- Every `data-autom` hook `select_config()` clicks exists on the page:
  `dimensionColor{starwhite,nightsky}`, `dimensionCapacity{256gb,512gb,1tb,2tb}`,
  `choose-noTradeIn`, `purchaseGroupOptionfullprice`,
  `carrierModel{ATT_IPHONEDUO,TMOBILE_IPHONEDUO,VERIZON_IPHONEDUO,UNLOCKED/US}`,
  `noapplecare`.
- Prices match ($1999 / $2199 / $2599 / $3199).

**So: do not go looking for a selector or catalog bug. There isn't one.**
`rehearse` selectors in `checkout.py` are still unverified (nothing got that far).

## The blocker

```
$ ./duo check
[11:49:43] Apple blocked the availability request (HTTP 541). Try again later.
```

`check_model()` waits for the page's *own* `/shop/fulfillment-messages`
request and reads its status, so this is Apple refusing a request the page
made — not us calling the API, and not a malformed call. A missing store
location would have produced a 200 with empty store data (the "no store data,
run setup" path), not a 541.

This was the **first** availability request ever made from this machine, on a
brand-new browser profile, signed out.

### Ruled out, with evidence

- **Not a bad URL.** The owner first hit a "Page Not Found" on
  `…/iphone-duo/7.6-inch-display-512gb-night-sky-att?product=MK1H4LL%2FA&cppart=ATT_IPHONEDUO`.
  That URL is Apple's own `seoUrlToken` for that config and loads fine.
- **Not the bot's browser or profile.** Driving the tracker's own
  `open_context()`, all of these returned 200 with `server: Apple`:
  `apple.com/`, `/shop/buy-iphone/`, `/shop/buy-iphone/iphone-duo` (colour
  radios present), plus the unlocked *and* AT&T deep config URLs.
- **Not a stale/locked profile.** Zero Chrome processes were running between
  attempts; the 404 reproduced across two fresh `setup` runs and the 200s came
  from the same profile minutes apart.
- **Inconsistent responses to identical URLs** (404 for the owner at 11:37–11:48,
  200 for scripted loads at 11:45 and 11:48) look like edge-level
  throttling, not a broken route.

### Not yet known

1. **Does Apple serve availability to a *non-automated* browser on this same
   home IP?** This is the decisive test and it had not been run when this was
   written. Owner opens their ordinary Chrome → buy page → Check availability
   → 11754.
   - Stores come back ⇒ the refusal targets automation. The Ubuntu box will be
     refused too (same IP, same Playwright posture, and Xvfb/Linux Chrome is
     *more* conspicuous, not less). The project's premise that a visible real
     browser on a home IP is acceptable to Apple no longer holds, and `submit`
     / `review` are not viable. Realistic answer: `alert` mode plus the hosted
     radar in the README, owner taps Buy.
   - It fails for them too ⇒ IP-level or Apple-side, automation irrelevant,
     waiting is the answer, and the Ubuntu box is equally affected.
2. **Was the Ubuntu box watching during all this?** Never confirmed. If
   `duo-tracker.service` was live it was hitting Apple from the same IP the
   whole time, which both violates CLAUDE.md's one-watcher rule and could
   explain an instant block. Check `systemctl --user status duo-tracker`.
3. Whether the 541 is transient. Only one data point exists.

## Out of bounds

CLAUDE.md's no-evasion rule stands and is not negotiable by any assistant:
no UA spoofing, no stripping `navigator.webdriver`, no stealth plugins, no
fingerprint/header randomisation, no CAPTCHA solving, no residential proxies,
no retry-until-the-challenge-clears. The owner asked during this session
whether those rules could be set aside; they were not, and the next session
should expect the same answer. Routing the bot through another network to get
a fresh IP falls on the same side of the line.

Legitimate levers, in rough order of value:

1. Wait out the backoff and retry — cheapest test of "permanent or not".
2. Confirm exactly one watcher exists anywhere.
3. Slow the schedule (`default_seconds` up, fast windows narrower). Fewer
   requests is cooperating with the rate limit, not hiding from it.
4. Drop to `checkout.mode: alert` and buy by hand. Also sidesteps the README's
   own warning that Apple cancels orders it believes a bot placed.

## Small real bug found, not yet fixed

`cmd_check` (`duo_tracker/cli.py:223`) catches `Blocked`, prints, and returns 2
— but never calls `sched.on_block()` the way `cmd_watch` does
(`cli.py:372`). So the 541 above is **not** in the block history and
`./duo blocks` still reports "No blocks logged yet. 🎉". That is misleading
exactly when the history matters most. One-line fix; the owner was asked and
had not answered.

Do **not** add 404 to `BLOCKED_STATUSES` on current evidence — 541 is the real
signal and it is already handled correctly.

## Reproducing the diagnosis

```bash
cd ~/Duo-tracker
./duo check          # the 541
./duo blocks         # shows nothing, per the bug above
tail -30 ~/.duo-tracker/logs/duo-tracker.log
```

Nothing is running, no watcher is installed, no `order.lock` exists, and no
order has ever been placed.
