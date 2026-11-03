#!/usr/bin/env bash
# Run Duo-tracker 24/7 on a headless Linux box:
#   duo-display  – virtual screen (Xvfb :99) the browser draws on
#   duo-vnc      – VNC server so you can see/drive that screen from your Mac or phone
#   duo-tracker  – the watcher; restarts on crash and after reboot
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
STATE="$HOME/.duo-tracker"
mkdir -p "$UNIT_DIR" "$STATE"

[ -x .venv/bin/python ] || { echo "Run ./install.sh first."; exit 1; }
[ -f config.yaml ] || { echo "No config.yaml — run ./duo init first."; exit 1; }

if [ ! -f "$STATE/vncpass" ]; then
  echo "Choose a VNC password (you'll type it on your Mac/phone to see the bot's screen):"
  x11vnc -storepasswd "$STATE/vncpass"
fi

cat > "$UNIT_DIR/duo-display.service" <<UNIT
[Unit]
Description=Duo-tracker virtual screen

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1440x900x24 -nolisten tcp
Restart=always

[Install]
WantedBy=default.target
UNIT

cat > "$UNIT_DIR/duo-vnc.service" <<UNIT
[Unit]
Description=Duo-tracker VNC (view/drive the bot's browser)
Requires=duo-display.service
After=duo-display.service

[Service]
ExecStart=/usr/bin/x11vnc -display :99 -forever -shared -rfbauth $STATE/vncpass -rfbport 5900 -quiet
Restart=always

[Install]
WantedBy=default.target
UNIT

cat > "$UNIT_DIR/duo-tracker.service" <<UNIT
[Unit]
Description=Duo-tracker watcher
Requires=duo-display.service
After=duo-display.service network-online.target

[Service]
WorkingDirectory=$REPO
Environment=DISPLAY=:99 PYTHONUNBUFFERED=1
ExecStart=$REPO/.venv/bin/python -m duo_tracker watch
Restart=on-failure
RestartSec=30
# 3 = config error / order already placed / no rehearsal: restarting won't help
RestartPreventExitStatus=3

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now duo-display duo-vnc
systemctl --user enable duo-tracker

echo "Letting your services run without you being logged in (sudo)…"
sudo loginctl enable-linger "$USER"

read -rp "Stop this box from ever sleeping/suspending? Recommended for a 24/7 watcher. (y/n) [y] " ans
if [ "${ans:-y}" = y ]; then
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<TXT

Installed. Next:
  1. ./duo setup       then connect a VNC viewer to vnc://${ip:-<this-box>}:5900 and sign in to Apple
                       (Mac: Finder → Go → Connect to Server; iPhone: a VNC app like RealVNC Viewer)
  2. ./duo rehearse    practice checkout; watch it over VNC if you like
  3. ./duo start       start watching (auto-starts after reboots too)
     ./duo logs        follow what it's doing;  ./duo status  for health
TXT
