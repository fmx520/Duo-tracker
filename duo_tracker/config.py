from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .catalog import CARRIERS, Model, resolve
from .schedule import Schedule, Window, parse_windows

MIN_INTERVAL_SECONDS = 30
DEFAULT_PROFILE = Path.home() / ".duo-tracker" / "profile"
CHECKOUT_MODES = ("alert", "bag", "review", "submit", "trigger")
# What a confirmed trigger does. submit still needs a passing rehearsal.
TRIGGER_ACTIONS = ("review", "submit")
TRIGGER_SOURCES = ("ntfy", "radar")
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
    zip: str = ""
    schedule_default: int = 30
    schedule_windows: tuple = ()
    schedule_tz: str = "America/New_York"
    trigger_server: str = "https://ntfy.sh"
    trigger_topic: str = ""
    trigger_secret: str = ""
    trigger_token: str = ""
    trigger_max_age_seconds: int = 90
    trigger_reconnect_backoff_seconds: int = 5
    trigger_action: str = "submit"
    trigger_sources: tuple = ("ntfy",)
    radar_url: str = "https://duo-tracker-six.vercel.app"
    radar_poll_seconds: float = 5

    def store_allowed(self, store_number: str) -> bool:
        """Is a store number inside the `stores:` filter? (No filter = any store.)"""
        if not self.store_filter:
            return True
        return store_number.strip().lower() in self.store_filter

    def make_schedule(self, log_path: Path | None = None) -> Schedule:
        return Schedule(self.schedule_default, list(self.schedule_windows), self.schedule_tz, log_path)

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
    sched = raw.get("schedule") or {}
    windows = parse_windows(sched.get("windows"))
    sched_default = int(sched.get("default_seconds", interval))
    tz = str(sched.get("timezone", "America/New_York"))
    try:
        Schedule(sched_default, windows, tz)
    except Exception as e:
        raise ValueError(str(e) if isinstance(e, ValueError) else f"config: bad schedule.timezone {tz!r}")
    zip_code = str(raw.get("zip") or "").strip()
    if zip_code and not (zip_code.isdigit() and len(zip_code) == 5):
        raise ValueError("config: zip must be a 5-digit US ZIP code")

    trig = raw.get("trigger") or {}
    ntfy_topic = str(ntfy.get("topic", ""))
    trig_topic = str(trig.get("topic") or "").strip()
    trig_secret = str(trig.get("secret") or "")
    trig_action = str(trig.get("action", "submit")).lower()
    if trig_action not in TRIGGER_ACTIONS:
        raise ValueError(f"config: trigger.action must be one of {', '.join(TRIGGER_ACTIONS)}")
    sources = trig.get("sources", ["ntfy"])
    sources = tuple(str(s).lower() for s in ([sources] if isinstance(sources, str) else sources or []))
    if not sources or any(s not in TRIGGER_SOURCES for s in sources):
        raise ValueError(f"config: trigger.sources must be a list drawn from {', '.join(TRIGGER_SOURCES)}")
    radar_poll = float(trig.get("radar_poll_seconds", 5))
    if radar_poll < 3:
        raise ValueError("config: trigger.radar_poll_seconds must be >= 3 (it's someone else's free service)")
    if mode == "trigger":
        # TRIGGER_MODE.md: refuse to start with an unauthenticated or shared inbound topic.
        if "ntfy" in sources and not trig_topic:
            raise ValueError("config: checkout.mode trigger needs trigger.topic (run `./duo trigger-init`)")
        if not trig_secret:
            raise ValueError("config: checkout.mode trigger needs trigger.secret (run `./duo trigger-init`)")
        if len(trig_secret) < 32:
            raise ValueError("config: trigger.secret is too short; use at least 32 random characters")
        if "ntfy" in sources and trig_topic == ntfy_topic:
            raise ValueError("config: trigger.topic must differ from ntfy.topic (the alert topic is not private)")
    max_age = int(trig.get("max_age_seconds", 90))
    if not 10 <= max_age <= 600:
        raise ValueError("config: trigger.max_age_seconds must be between 10 and 600")
    return Config(
        models=models,
        carrier=CARRIERS[carrier_key],
        interval_seconds=interval,
        store_filter=[s.lower() for s in raw.get("stores") or []],
        ntfy_server=str(ntfy.get("server", "https://ntfy.sh")),
        ntfy_topic=ntfy_topic,
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
        zip=zip_code,
        schedule_default=sched_default,
        schedule_windows=tuple(windows),
        schedule_tz=tz,
        trigger_server=str(trig.get("server") or ntfy.get("server") or "https://ntfy.sh"),
        trigger_topic=trig_topic,
        trigger_secret=trig_secret,
        trigger_token=str(trig.get("token") or ""),
        trigger_max_age_seconds=max_age,
        trigger_reconnect_backoff_seconds=max(1, int(trig.get("reconnect_backoff_seconds", 5))),
        trigger_action=trig_action,
        trigger_sources=sources,
        radar_url=str(trig.get("radar_url") or "https://duo-tracker-six.vercel.app"),
        radar_poll_seconds=radar_poll,
    )


def _opt_hour(v) -> int | None:
    if v is None or v is False:
        return None
    h = int(v)
    if not 0 <= h <= 23:
        raise ValueError("config: health.daily_summary_hour must be 0–23 (or null to disable)")
    return h
