"""When to check: faster during the windows drops actually land, slower otherwise.

Defaults come from the drop history of the stores near 11754 (Oct 26 – Nov 2):
nearly every Long Island drop landed 7:30–9:30am ET, with a smaller bump
around midnight ET.

Adaptive: if Apple blocks us inside a fast window, that window falls back to
the slow rate until it ends, so we don't keep hammering after a "no".
Every block is appended to ~/.duo-tracker/blocks.csv so the rates can be
tuned to what your connection actually tolerates (`./duo blocks`).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MIN_SECONDS = 30
BLOCK_LOG_FIELDS = ["at", "reason", "interval_s", "window", "minutes_since_last_block"]


@dataclass(frozen=True)
class Window:
    start: time
    end: time
    seconds: int

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"

    def occurrence(self, t: datetime) -> date | None:
        """Date this window opened on, if ``t`` (local) is inside it; handles midnight wrap."""
        now = t.time()
        if self.start <= self.end:
            return t.date() if self.start <= now < self.end else None
        if now >= self.start:
            return t.date()
        if now < self.end:
            return t.date() - timedelta(days=1)
        return None


def _hm(s: str) -> time:
    try:
        h, m = str(s).strip().split(":")
        return time(int(h), int(m))
    except Exception:
        raise ValueError(f"config: schedule time {s!r} must look like 07:30")


def parse_windows(raw: list | None) -> list[Window]:
    out = []
    for w in raw or []:
        secs = int(w.get("seconds", MIN_SECONDS))
        if secs < MIN_SECONDS:
            raise ValueError(f"config: schedule window seconds must be >= {MIN_SECONDS}")
        out.append(Window(_hm(w["start"]), _hm(w["end"]), secs))
    return out


class Schedule:
    def __init__(self, default_seconds: int, windows: list[Window], tz: str = "America/New_York",
                 log_path: Path | None = None):
        if default_seconds < MIN_SECONDS:
            raise ValueError(f"config: schedule default_seconds must be >= {MIN_SECONDS}")
        self.default = default_seconds
        self.windows = windows
        self.tz = ZoneInfo(tz)
        self.log_path = log_path
        self._degraded: set[tuple[int, date]] = set()
        self._last_block: datetime | None = None

    def _local(self, now: datetime | None) -> datetime:
        return (now or datetime.now(self.tz)).astimezone(self.tz)

    def current(self, now: datetime | None = None) -> tuple[int, str]:
        """(seconds until next round, human label of why)."""
        t = self._local(now)
        for i, w in enumerate(self.windows):
            occ = w.occurrence(t)
            if occ is None:
                continue
            if (i, occ) in self._degraded:
                return self.default, f"peak {w.label} (slowed after a block)"
            return w.seconds, f"peak {w.label}"
        return self.default, "off-peak"

    def on_block(self, reason: str, now: datetime | None = None) -> None:
        t = self._local(now)
        interval, label = self.current(t)
        for i, w in enumerate(self.windows):
            occ = w.occurrence(t)
            if occ is not None:
                self._degraded.add((i, occ))
        since = round((t - self._last_block).total_seconds() / 60, 1) if self._last_block else ""
        self._last_block = t
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.log_path.exists()
            with self.log_path.open("a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(BLOCK_LOG_FIELDS)
                w.writerow([t.isoformat(timespec="seconds"), reason, interval, label, since])


def summarize_blocks(path: Path) -> str:
    if not path.exists():
        return "No blocks logged yet. 🎉"
    rows = list(csv.DictReader(path.open()))
    if not rows:
        return "No blocks logged yet. 🎉"
    by_rate: dict[str, int] = {}
    for r in rows:
        by_rate[r["interval_s"]] = by_rate.get(r["interval_s"], 0) + 1
    days = {r["at"][:10] for r in rows}
    gaps = [float(r["minutes_since_last_block"]) for r in rows if r["minutes_since_last_block"]]
    lines = [f"{len(rows)} blocks over {len(days)} day(s); last at {rows[-1]['at']} ({rows[-1]['window']})."]
    lines += [f"  checking every {k:>3}s when blocked: {v}×" for k, v in sorted(by_rate.items(), key=lambda kv: int(kv[0]))]
    if gaps:
        lines.append(f"  typical time between blocks: {sorted(gaps)[len(gaps) // 2]:.0f} min")
    lines.append("Tip: if most blocks happen at the fast rate, raise the peak `seconds` a little; "
                 "if there are none for a week, you could try lowering it.")
    return "\n".join(lines)
