from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .catalog import CARRIERS, Model, resolve

MIN_INTERVAL_SECONDS = 30
DEFAULT_PROFILE = Path.home() / ".duo-tracker" / "profile"
CHECKOUT_MODES = ("alert", "bag", "checkout")


@dataclass
class Config:
    models: list[Model]
    carrier: str
    interval_seconds: int
    store_filter: list[str]
    ntfy_server: str
    ntfy_topic: str
    checkout_mode: str
    profile_dir: Path
    browser_channel: str
    blocked_backoff_seconds: int


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    models = [resolve(k) for k in raw.get("models") or []]
    if not models:
        raise ValueError("config: list at least one model under `models:` (run `python -m duo_tracker models`)")

    carrier_key = str(raw.get("carrier", "unlocked")).lower()
    if carrier_key not in CARRIERS:
        raise ValueError(f"config: carrier must be one of {', '.join(CARRIERS)}")

    interval = int(raw.get("interval_seconds", 60))
    if interval < MIN_INTERVAL_SECONDS:
        raise ValueError(f"config: interval_seconds must be >= {MIN_INTERVAL_SECONDS} (be polite to apple.com)")

    mode = str((raw.get("checkout") or {}).get("mode", "bag")).lower()
    if mode not in CHECKOUT_MODES:
        raise ValueError(f"config: checkout.mode must be one of {', '.join(CHECKOUT_MODES)}")

    ntfy = raw.get("ntfy") or {}
    browser = raw.get("browser") or {}
    return Config(
        models=models,
        carrier=CARRIERS[carrier_key],
        interval_seconds=interval,
        store_filter=[s.lower() for s in raw.get("stores") or []],
        ntfy_server=str(ntfy.get("server", "https://ntfy.sh")),
        ntfy_topic=str(ntfy.get("topic", "")),
        checkout_mode=mode,
        profile_dir=Path(browser.get("profile_dir") or DEFAULT_PROFILE).expanduser(),
        browser_channel=str(browser.get("channel", "chrome")),
        blocked_backoff_seconds=max(300, int(raw.get("blocked_backoff_seconds", 600))),
    )
