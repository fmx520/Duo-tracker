#!/usr/bin/env bash
# Linux readiness check for the anti-bot layer. Safe to run: opens the bot's
# browser on the virtual screen and loads the buy page, but never buys anything.
#   ./scripts/linux-smoke.sh          # checks launch + page load
#   ./scripts/linux-smoke.sh --fetch  # also asks Apple for stock once (hits apple.com)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✔\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✘\033[0m %s\n' "$*"; FAIL=1; }
FAIL=0

say "1. Python + Playwright"
"$PY" - <<'PYEOF' && ok "Python 3.10+, playwright importable" || bad "Python/playwright problem"
import sys
assert sys.version_info >= (3, 10), f"need Python 3.10+, got {sys.version.split()[0]}"
import playwright.sync_api  # noqa
PYEOF

say "2. Screen (the bot draws its browser on a display)"
if [ -n "${DISPLAY:-}" ]; then
  ok "DISPLAY=$DISPLAY"
elif systemctl --user is-active -q duo-display 2>/dev/null; then
  export DISPLAY=:99
  ok "using the service's virtual screen DISPLAY=:99"
else
  bad "no DISPLAY and duo-display not running — run scripts/install-linux-service.sh (or prefix: xvfb-run -a)"
fi

say "3. Browser binary"
CH="$( "$PY" - <<'PYEOF'
from duo_tracker.config import load
try:
    print(load("config.yaml").browser_channel)
except Exception:
    print("chrome")
PYEOF
)"
case "$CH" in
  chrome)  BIN=/opt/google/chrome/chrome ;;
  msedge)  BIN=/usr/bin/microsoft-edge ;;
  *)       BIN="" ;;
esac
if [ -n "$BIN" ] && [ -x "$BIN" ]; then ok "found $CH at $BIN";
elif [ -z "$BIN" ]; then ok "channel '$CH' uses Playwright's bundled Chromium"
else bad "channel is '$CH' but $BIN not found — run: $PY -m playwright install --with-deps $CH"; fi

say "4. Chrome sandbox kernel setting"
US=/proc/sys/kernel/unprivileged_userns_clone
if [ -f "$US" ] && [ "$(cat "$US")" = 0 ]; then
  echo "  ! kernel.unprivileged_userns_clone=0 — the bot auto-falls back to no-sandbox."
  echo "    to keep the sandbox:  sudo sysctl kernel.unprivileged_userns_clone=1"
else
  ok "unprivileged user namespaces allowed (Chrome sandbox can stay on)"
fi

say "5. Launch the bot's browser and load the buy page (on ${DISPLAY:-no display})"
"$PY" - <<'PYEOF' && ok "browser launched, buy page loaded" || bad "launch/page-load failed (see message above)"
from playwright.sync_api import sync_playwright
from duo_tracker.config import load
from duo_tracker.browser import open_context, load_buy_page, _page
cfg = load("config.yaml")
with sync_playwright() as pw:
    ctx = open_context(pw, cfg)
    page = _page(ctx)
    load_buy_page(page)
    print("  page title:", (page.title() or "")[:60])
    ctx.close()
PYEOF

if [ "${1:-}" = "--fetch" ]; then
  say "6. One live availability request (hits apple.com)"
  "$PY" - <<'PYEOF' && ok "availability request returned (no 541 on first try)" || bad "blocked/errored — see message"
from playwright.sync_api import sync_playwright
from duo_tracker.config import load
from duo_tracker.browser import Blocked, active_fetch, open_context, load_buy_page, _page
cfg = load("config.yaml")
with sync_playwright() as pw:
    ctx = open_context(pw, cfg)
    page = _page(ctx)
    load_buy_page(page)
    try:
        d = active_fetch(page, cfg.models[0].part, cfg.zip)
        print("  HTTP 200; response keys:", list(d)[:4])
    except Blocked as e:
        print("  blocked:", e); raise SystemExit(1)
    ctx.close()
PYEOF
fi

echo
[ "$FAIL" = 0 ] && { say "All good — the evasion layer can run on this box."; exit 0; } || { say "Fix the ✘ items above, then re-run."; exit 1; }
