"""Keep you told when the watcher is NOT working, which is the dangerous case.

* Daily summary: "still watching, N checks, M drops" at a set hour.
* Stall alert: no successful check for N minutes (once, then "recovered").
* Dead-man switch: optional healthchecks.io-style URL pinged after successful
  checks (or, in trigger mode, while the trigger subscription is connected). If the box loses power or internet, *that* service alerts you,
  because a dead box can't alert anyone itself.
"""
from __future__ import annotations

import logging
import socket
import time
from datetime import datetime
from typing import Callable

import requests

log = logging.getLogger(__name__)


class Health:
    def __init__(self, notifier, *, daily_hour: int | None = 9, stall_minutes: int = 10,
                 healthcheck_url: str = "", clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = datetime.now, http_get=None, kind: str = "poll"):
        self.notifier = notifier
        self.kind = kind  # "poll": healthy = checks succeed; "trigger": healthy = subscription connected
        self.daily_hour = daily_hour
        self.stall_s = stall_minutes * 60
        self.hc_url = healthcheck_url.rstrip("/")
        self.clock, self.now = clock, now
        self.http_get = http_get or (lambda url: requests.get(url, timeout=5))
        self.started = clock()
        self.last_success = clock()
        self.last_problem = ""
        self.stall_notified = False
        self.checks = self.drops = self.blocks = 0
        self.triggers = self.rejected = 0
        self.last_ping = 0.0
        self.last_summary_day = now().date() if daily_hour is not None and now().hour >= daily_hour else None

    # ── events ────────────────────────────────────────────────────────────────
    def success(self) -> None:
        self.checks += 1
        self.last_success = self.clock()
        if self.stall_notified:
            self.stall_notified = False
            self.notifier.send("Duo-tracker recovered", "Checks are succeeding again.", urgent=False, tags="white_check_mark")
            self.ping(force=True)  # clear the dead-man switch's "down" state right away
            return
        self.ping()

    def alive(self) -> None:
        """Trigger mode heartbeat: the subscription is connected (open or keepalive)."""
        self.last_success = self.clock()
        if self.stall_notified:
            self.stall_notified = False
            self.notifier.send("Duo-tracker recovered", "Trigger listener is connected again.",
                               urgent=False, tags="white_check_mark")
            self.ping(force=True)
            return
        self.ping()

    def trigger(self, accepted: bool) -> None:
        if accepted:
            self.triggers += 1
        else:
            self.rejected += 1

    def problem(self, reason: str) -> None:
        self.last_problem = reason

    def already_notified(self, reason: str) -> None:
        """A problem you've already been told about (e.g. Apple block back-off): don't double-alert."""
        self.last_problem = reason
        self.stall_notified = True
        self.blocks += 1

    def drop(self) -> None:
        self.drops += 1

    # ── periodic ──────────────────────────────────────────────────────────────
    def tick(self) -> None:
        t = self.clock()
        if not self.stall_notified and t - self.last_success >= self.stall_s:
            self.stall_notified = True
            mins = int((t - self.last_success) // 60)
            why = f" Last problem: {self.last_problem}" if self.last_problem else ""
            what = "Trigger listener disconnected" if self.kind == "trigger" else "No successful check"
            self.notifier.send("⚠️ Duo-tracker stalled", f"{what} for {mins} min.{why}",
                               urgent=False, tags="warning")
            self.ping(fail=True)

        now = self.now()
        if self.daily_hour is not None and now.hour >= self.daily_hour and self.last_summary_day != now.date():
            self.last_summary_day = now.date()
            state = "⚠️ currently stalled" if self.stall_notified else "healthy"
            if self.kind == "trigger":
                counts = (f"{self.triggers} triggers accepted, {self.rejected} rejected, "
                          f"{self.drops} confirmed drops, {self.blocks} Apple blocks")
            else:
                counts = f"{self.checks} checks, {self.drops} drops, {self.blocks} Apple blocks"
            self.notifier.send(
                "Duo-tracker daily check-in",
                f"{socket.gethostname()}: {state}. {counts} since last check-in.",
                urgent=False, tags="calendar")
            self.checks = self.drops = self.blocks = 0
            self.triggers = self.rejected = 0

    def ping(self, fail: bool = False, force: bool = False) -> bool:
        """Ping the dead-man switch. True only if a ping actually went out.

        False means no URL configured, throttled, or the request failed --
        `notify-test` uses force=True so it only ever means "it failed".
        """
        if not self.hc_url:
            return False
        t = self.clock()
        if not (fail or force) and t - self.last_ping < 60:
            return False
        self.last_ping = t
        try:
            self.http_get(self.hc_url + ("/fail" if fail else ""))
            return True
        except Exception as e:  # never let monitoring break watching
            log.warning("healthcheck ping failed: %s", e)
            return False
