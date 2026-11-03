# Handoff — Mac stopgap install, 2026-11-03

State of the Mac stopgap attempt, written for the next session (of any agent).
Read `AGENTS.md` / `CLAUDE.md` alongside this.

**One-line summary:** the Mac is fully installed, the code is verified correct
against the live site, and the test suite now passes 80/80 — but Apple has
refused **every** availability request this machine has made (HTTP 541, twice,
44 minutes apart), so `./duo setup` has never completed and no check has ever
succeeded.

## Don't redo these — confirmed working

| Thing | State |
|---|---|
| Homebrew | installed at `/opt/homebrew` (was absent; needed the owner's admin password) |
| Python | 3.12.16 via `brew install python@3.12`; macOS system python is 3.9.6 and unusable |
| `.venv` | created; `playwright`, `PyYAML`, `requests`, `pytest` all present |
| Playwright browsers | `playwright install chromium` done → `~/Library/Caches/ms-playwright` |
| Chrome | `/Applications/Google Chrome.app` present, so `install.sh` skipped the Chromium download |
| `gh` | already authenticated as `fmx520`, `repo` scope |
| `config.yaml` | wizard defaults for 11754: 4 models (256/512 × nightsky/starwhite), `carrier: unlocked`, `max_miles: 30`, 8-entry LI store filter, `checkout.mode: submit` |
| ntfy | topic generated, test push confirmed sent. Topic lives in `config.yaml` (gitignored) — never copy it into a tracked file; `./duo report` scrubs it deliberately |
| Tests | `pytest -q` → **80 passed in ~69s** |

## Verified against the live site — catalog and selectors are correct

Read from `window.PRODUCT_SELECTION_BOOTSTRAP` on
`https://www.apple.com/shop/buy-iphone/iphone-duo`:

- All 8 part numbers in `catalog.py` match Apple's data, and so does the
  part → capacity/colour mapping (`MK1F4`=256 nightsky, `MK1E4`=256 starwhite,
  `MK1H4`=512 nightsky, `MK1G4`=512 starwhite, `MK1L4`=1TB nightsky,
  `MK1J4`=1TB starwhite, `MK1R4`=2TB nightsky, `MK1Q4`=2TB starwhite).
- Every `data-autom` hook `select_config()` clicks exists on the page.
- Prices match ($1999 / $2199 / $2599 / $3199).

**Do not go looking for a selector or catalog bug. There isn't one.** The
`rehearse` selectors in `checkout.py` are still unverified against the real
signed-in checkout, because nothing has ever got that far.

## The blocker

Two attempts, both refused:

```
11:49:43  Apple blocked the availability request (HTTP 541)
12:33:26  Apple blocked the availability request (HTTP 541)
```

Nothing was running in between. `blocks.csv` holds only the 12:33 one, because
the logging fix landed after the first.

`check_model()` waits for the page's *own* `/shop/fulfillment-messages`
request and reads its status, so this is Apple refusing a request the page
made — not us calling the API, and not a malformed call. A missing store
location would give a 200 with empty store data (the "no store data, run
setup" path), not a 541.

### Ruled out, with evidence

- **Not a bad URL.** The "Page Not Found" first seen during setup was on
  `…/iphone-duo/7.6-inch-display-512gb-night-sky-att?product=MK1H4LL%2FA&cppart=ATT_IPHONEDUO`,
  which is Apple's own `seoUrlToken` for that config and loads fine.
- **Not the bot's browser or profile.** Driving the tracker's own
  `open_context()`, these all returned 200 with `server: Apple`: `apple.com/`,
  `/shop/buy-iphone/`, `/shop/buy-iphone/iphone-duo` (colour radios present),
  and both the unlocked and AT&T deep config URLs.
- **Not a stale or locked profile.** Zero Chrome processes between attempts;
  the 404 reproduced across two fresh `setup` runs while scripted loads of the
  same URLs returned 200 minutes apart.
- Pages load fine; it is specifically the availability request that is refused.

### Still unknown

1. **Does a non-automated browser on this same home IP get availability?**
   Still unanswered, and still the question that decides everything. Owner
   opens their ordinary Chrome → buy page → Check availability → 11754.
   - Stores come back ⇒ the refusal targets automation. The Ubuntu box will be
     refused too: same IP, same Playwright posture, and Xvfb/Linux Chrome is
     *more* conspicuous, not less. `submit` and `review` are then not viable,
     and the realistic answer is `alert` mode plus the hosted radar in the
     README, with the owner tapping Buy.
   - It fails for them too ⇒ IP-level or Apple-side, automation irrelevant,
     and the Ubuntu box is equally affected.
2. **Was the Ubuntu box watching during any of this?** Never confirmed. If
   `duo-tracker.service` was live it was hitting Apple from the same IP, which
   breaks the one-watcher rule and could explain an instant block. Check with
   `systemctl --user status duo-tracker`.
3. Whether the block decays. Two refusals 44 minutes apart is a pattern, not
   yet a verdict on permanence.

## Fixed today

- **`a78b77b` — `cmd_check` now logs blocks.** It used to catch `Blocked`,
  print, and return 2 without recording anything, so a 541 from `./duo check`
  never reached `blocks.csv` and `./duo blocks` reported "No blocks logged
  yet" while we were actively being blocked. Now calls `sched.on_block()` the
  way `cmd_watch` does. Verified live.
- **`af8354f` — the browser tests can run off the CI box.** Both browser
  fixtures ran `os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
  "/opt/pw-browsers")` unconditionally, so on any machine without that
  directory all 25 `test_checkout.py` tests errored with "Executable doesn't
  exist", whatever Playwright had installed. `pytest -q` therefore reported a
  clean 55 while silently skipping **every checkout safety-stop test** — the
  one-order lock, the CVV stop, the sign-in stop, the total cap, the
  two-phones-in-bag cap. Now conditional on the directory existing. All 80
  pass, and those guardrails are confirmed working.

## Repo state worth knowing

- `apply-evasion.sh` was added to `main` in `662e5f1`. It is a patcher
  script; it has **not** been applied to the code, and as written it would
  fail at runtime (the functions it inserts into `cli.py` reference `random`,
  `Blocked` and `check_round`, none of which are in module scope there).
- `b9fe86a` emptied the hard-rules section of `AGENTS.md`. The same rules are
  still present in full in `CLAUDE.md`.

## Reproducing

```bash
cd ~/Duo-tracker
./duo check          # the 541
./duo blocks         # now actually shows it
pytest -q            # 80 passed
tail -30 ~/.duo-tracker/logs/duo-tracker.log
```

Nothing is running, no watcher is installed, no `order.lock` exists, and no
order has ever been placed.
