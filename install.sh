#!/usr/bin/env bash
# One-time install for Ubuntu/Debian or macOS:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ "$(uname -s)" = Linux ]; then
  say "Installing system packages: Python venv support, virtual screen, VNC (needs sudo)…"
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-pip xvfb x11vnc
fi

# Use the newest Python 3.10+ available (e.g. python3.11 from the deadsnakes PPA on Ubuntu 20.04).
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  cat <<'TXT'
Python 3.10+ is needed, and this system only has an older one.
  Ubuntu 20.04: either upgrade (sudo do-release-upgrade → 22.04), or add a newer Python:
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt update && sudo apt install -y python3.11 python3.11-venv
  then run ./install.sh again.
TXT
  exit 1
fi
echo "Using $PY ($("$PY" --version 2>&1))."

say "Creating virtualenv (.venv) and installing Python packages…"
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

case "$(uname -s)" in
  Linux)
    say "Installing Google Chrome for the bot (falls back to Chromium)…"
    .venv/bin/playwright install --with-deps chrome || .venv/bin/playwright install --with-deps chromium
    ;;
  Darwin)
    if [ -d "/Applications/Google Chrome.app" ]; then
      echo "Using your installed Google Chrome."
    else
      say "Google Chrome not found — installing Playwright's Chromium instead…"
      .venv/bin/playwright install chromium
    fi
    chmod +x "Start Duo Tracker.command"
    ;;
esac
chmod +x duo scripts/*.sh

if [ ! -f config.yaml ]; then
  say "Let's set it up."
  ./duo init
fi

say "Installed."
case "$(uname -s)" in
  Linux)  echo "Next: scripts/install-linux-service.sh   (virtual screen, VNC, auto-start)";;
  Darwin) echo "Next: ./duo setup  →  ./duo rehearse  →  double-click 'Start Duo Tracker.command'";;
esac
