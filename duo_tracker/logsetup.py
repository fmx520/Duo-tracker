"""One log for everything: concise on screen, detailed in a rotating file.

~/.duo-tracker/logs/duo-tracker.log   today (full detail, incl. error tracebacks)
~/.duo-tracker/logs/duo-tracker.log.YYYY-MM-DD   previous days, 7 kept
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = "duo_tracker"


def log_path(state_dir: Path) -> Path:
    return state_dir / "logs" / "duo-tracker.log"


def setup(state_dir: Path | None) -> logging.Logger:
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    if state_dir is not None:
        path = log_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(path, when="midnight", backupCount=7, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
    return root
