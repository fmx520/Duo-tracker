#!/usr/bin/env bash
# macOS: double-click to start watching. Keeps the Mac awake while it runs.
#
# In trigger mode with the `ntfy` source, it also starts the kelindi/apple-stock
# detector (DUO_DETECTOR_DIR, default ~/src/apple-stock) and stops it when the
# watcher stops -- so the detector never polls Apple while nothing listens, and
# never runs alongside this bot's own polling modes.
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
DETECTOR_DIR="${DUO_DETECTOR_DIR:-$HOME/src/apple-stock}"
LOG_DIR="$HOME/.duo-tracker/logs"

wants_detector=$("$PY" -c '
from duo_tracker.config import load
c = load("config.yaml")
print("yes" if c.checkout_mode == "trigger" and "ntfy" in c.trigger_sources else "no")
' 2>/dev/null || echo no)

detector_pid=""
if [ "$wants_detector" = yes ]; then
  if [ -f "$DETECTOR_DIR/src/trigger.js" ] && [ -f "$DETECTOR_DIR/.env" ]; then
    mkdir -p "$LOG_DIR"
    echo "Starting the apple-stock detector (log: $LOG_DIR/detector.log)…"
    (cd "$DETECTOR_DIR" && exec node --env-file=.env src/index.js) >>"$LOG_DIR/detector.log" 2>&1 &
    detector_pid=$!
    trap '[ -n "$detector_pid" ] && kill "$detector_pid" 2>/dev/null && echo "Detector stopped."' EXIT INT TERM
  else
    echo "Trigger mode wants the detector, but $DETECTOR_DIR isn't set up"
    echo "(needs src/trigger.js and .env — run ./duo trigger-env). Continuing with the radar only."
  fi
fi

caffeinate -dims ./duo watch
