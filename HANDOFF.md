# Handoff — Mac stopgap attempt, 2026-11-03

Written for the next session (of any agent). Read `AGENTS.md` / `CLAUDE.md`
alongside this.

**Verdict: Apple refuses this tool's automated browser. The approach does not
work from this network, and will not work from the Ubuntu box either.** The
install is complete, the code is verified correct, and the test suite passes
80/80 — the blocker is not a bug in this repo.

## The evidence

Three availability requests, three refusals, spread over 90 minutes with
nothing else running:

| Time | Result |
|---|---|
| 11:49:43 | HTTP 541 |
| 12:33:26 | HTTP 541 |
| 13:19:42 | HTTP 541 (after enabling Chrome's sandbox) |

Meanwhile, **in the owner's ordinary Chrome, on the same machine and the same
home IP: sign-in works, the bag works, availability works.**

What that isolates:

| Hypothesis | Verdict |
|---|---|
| Bad URL / stale catalog | **Ruled out.** All 8 part numbers, the part→config map and every `data-autom` selector match Apple's live data. The config URL that 404'd is Apple's own `seoUrlToken`. |
| The home IP is blocked | **Ruled out.** Ordinary Chrome on the same IP works completely. |
| Stale or locked browser profile | **Ruled out.** Reproduced across four fresh `setup` runs with no Chrome processes in between. |
| Checking before `setup` completed | **Ruled out.** A missing store location returns 200 with empty store data, not 541. The page's own `fulfillment-messages` request is what gets refused. |
| `--no-sandbox` flag | **Ruled out.** Removed in `bbc1db4`; the next check was refused identically. The flag is local and never reaches the site. |
| Apple refuses the automated session | **Consistent with everything observed.** |

The remaining difference between the working browser and the refused one is
that the latter is driven over CDP and reports `navigator.webdriver: true`.
Symptoms vary by surface and over time — 541 on availability, an intermittent
404 on config pages, and a sign-in widget that renders in 1s for a scripted
navigation but spins indefinitely in the same window for a human.

## Why no mode of this tool gets around that

`alert`, `bag`, `review` and `submit` all depend on the same call:
`check_model()` waits for the page's own `/shop/fulfillment-messages` request
and reads it. `checkout.mode` only decides what happens *after* a drop is
detected. If that request is refused, the watcher backs off, pushes "paused",
and never detects anything. **There is no configuration of this repo that
works while Apple refuses that request.**

The Ubuntu box shares the IP and uses the same Playwright-driven Chrome, on a
virtual screen that is if anything more conspicuous. There is no reason to
expect a different result there, and setting it up would be re-running this
afternoon with more steps.

## What to do instead

Subscribe to the hosted NYC radar the README already cites
(`duo-tracker-six.vercel.app`) and buy by hand when it pings. Someone else's
service does the detection; nothing on this network automates Apple. This also
sidesteps the README's own warning that Apple cancels orders it decides a bot
placed — a human tapping Buy on a drop they were alerted to carries none of
that risk.

The ntfy plumbing in this repo works (test push confirmed), so if alerts from
another source can be relayed into the same topic, the phone-notification half
of the original plan survives.

## Out of bounds

`CLAUDE.md`'s no-evasion rule: no UA spoofing, no removing automation flags or
`navigator.webdriver`, no stealth plugins, no fingerprint or header
randomisation, no decoy requests, no CAPTCHA solving, no residential proxies,
no retry-until-the-challenge-clears loops, and no switching networks to shed a
block. The owner asked during this session whether those could be set aside;
they were not. Any assistant will decline this work, so a next session should
not plan around doing it.

`apply-evasion.sh` (`662e5f1`) is in the repo root. It has **not** been applied
to the code, and as written it would fail at runtime: the functions it inserts
into `cli.py` reference `random`, `Blocked` and `check_round`, none of which
are in module scope there. `b9fe86a` emptied the hard-rules section of
`AGENTS.md`; the same rules remain in full in `CLAUDE.md`.

## Don't redo these — confirmed working

| Thing | State |
|---|---|
| Homebrew | `/opt/homebrew` (was absent; needed the owner's admin password) |
| Python | 3.12.16 via `brew install python@3.12`; system python is 3.9.6 and unusable |
| `.venv` | `playwright`, `PyYAML`, `requests`, `pytest` |
| Playwright browsers | installed → `~/Library/Caches/ms-playwright` |
| `config.yaml` | 11754 defaults, 4 models, `carrier: unlocked`, `max_miles: 30`, LI store filter, `checkout.mode: submit` |
| ntfy | topic generated, test push confirmed. Topic is in `config.yaml` (gitignored) — never copy it into a tracked file; `./duo report` scrubs it deliberately |
| Tests | `pytest -q` → **80 passed in ~67s** |
| Browser profile | `~/.duo-tracker/profile` exists but has **never been signed in** |

Verified against the live page: all 8 part numbers, the part→capacity/colour
map, every `data-autom` hook `select_config()` uses, and the prices. **Do not
go looking for a selector or catalog bug.** The `rehearse` selectors in
`checkout.py` remain unverified, because nothing ever got that far.

## Fixed today

- **`a78b77b`** — `cmd_check` now logs blocks. It used to catch `Blocked`,
  print and return without recording, so `./duo blocks` said "No blocks logged
  yet" while we were actively being blocked.
- **`af8354f`** — the browser tests can run off the CI box. Both fixtures set
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` unconditionally, so on any other
  machine all 25 `test_checkout.py` tests errored out. `pytest -q` reported a
  clean 55 while silently skipping **every checkout safety-stop test**. Now
  conditional; all 80 pass and those guardrails are confirmed working.
- **`bbc1db4`** — the bot's Chrome runs with its sandbox enabled. A security
  fix, not a block workaround; the next check was refused identically.

## Reproducing

```bash
cd ~/Duo-tracker
./duo check          # HTTP 541
./duo blocks         # 3 blocks logged
pytest -q            # 80 passed
tail -40 ~/.duo-tracker/logs/duo-tracker.log
```

Nothing is running, no watcher is installed, no `order.lock` exists, and no
order has ever been placed.
