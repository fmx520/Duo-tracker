from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .catalog import CARRIERS, Model, resolve

MIN_INTERVAL_SECONDS = 30
DEFAULT_PROFILE = Path.home() / ".duo-tracker" / "profile"
CHECKOUT_MODES = ("alert", "bag", "review", "submit")
DEFAULT_TAX_HEADROOM = 1.11  # highest US combined sales tax is ~10.25%


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
    max_miles: float | None
    alert_on_courier: bool
    max_total: float | None
    daily_summary_hour: int | None = 9
    stall_minutes: int = 10
    healthcheck_url: str = ""
    signin_check_minutes: int = 60

    def cap_for(self, model: Model) -> float:
        return self.max_total if self.max_total is not None else round(model.price * DEFAULT_TAX_HEADROOM, 2)


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    models = list({m.part: m for m in (resolve(k) for k in raw.get("models") or [])}.values())
    if not models:
        raise ValueError("config: list at least one model under `models:` (run `python -m duo_tracker models`)")

    carrier_key = str(raw.get("carrier", "unlocked")).lower()
    if carrier_key not in CARRIERS:
        raise ValueError(f"config: carrier must be one of {', '.join(CARRIERS)}")

    interval = int(raw.get("interval_seconds", 30))
    if interval < MIN_INTERVAL_SECONDS:
        raise ValueError(f"config: interval_seconds must be >= {MIN_INTERVAL_SECONDS} (be polite to apple.com)")

    checkout = raw.get("checkout") or {}
    mode = str(checkout.get("mode", "bag")).lower()
    mode = {"checkout": "review"}.get(mode, mode)
    if mode not in CHECKOUT_MODES:
        raise ValueError(f"config: checkout.mode must be one of {', '.join(CHECKOUT_MODES)}")

    ntfy = raw.get("ntfy") or {}
    browser = raw.get("browser") or {}
    health = raw.get("health") or {}
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
        max_miles=float(raw["max_miles"]) if raw.get("max_miles") is not None else None,
        alert_on_courier=bool(raw.get("alert_on_courier", True)),
        max_total=float(checkout["max_total"]) if checkout.get("max_total") is not None else None,
        daily_summary_hour=_opt_hour(health.get("daily_summary_hour", 9)),
        stall_minutes=max(3, int(health.get("stall_minutes", 10))),
        healthcheck_url=str(health.get("healthcheck_url") or ""),
        signin_check_minutes=max(15, int(raw.get("signin_check_minutes", 60))),
    )


def _opt_hour(v) -> int | None:
    if v is None or v is False:
        return None
    h = int(v)
    if not 0 <= h <= 23:
        raise ValueError("config: health.daily_summary_hour must be 0–23 (or null to disable)")
    return h
