#!/usr/bin/env bash
# One-time install for Ubuntu/Debian or macOS:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

command -v python3 >/dev/null || { echo "python3 not found — install Python 3.10+ first."; exit 1; }

say "Creating virtualenv (.venv) and installing Python packages…"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

case "$(uname -s)" in
  Linux)
    say "Installing the virtual screen + VNC server (needs sudo)…"
    sudo apt-get update -qq
    sudo apt-get install -y -qq xvfb x11vnc
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
