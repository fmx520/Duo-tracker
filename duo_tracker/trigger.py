"""Trigger mode: an external detector says "a Duo dropped", the bot confirms and buys.

See TRIGGER_MODE.md. The security rules there are not optional:

* a separate inbound ntfy topic (never the alert topic),
* every trigger HMAC-SHA256 signed with a shared secret (plus an optional
  ntfy access token on the subscription),
* ``seen_at`` freshness (``max_age_seconds``) and a one-use ``nonce``,
* fail closed: anything that doesn't validate is logged, alerted, and ignored.

A valid trigger is a *hint*. The caller still runs its own availability check
and only buys if Apple agrees the named store has the part.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import requests

log = logging.getLogger(__name__)

VERSION = 1
# Detector and bot clocks are both NTP-synced, but allow a little drift before
# calling a timestamp "from the future".
FUTURE_SKEW_SECONDS = 5
NONCE_KEEP_SECONDS = 24 * 3600


class TriggerRejected(Exception):
    """A trigger failed validation. Treat as a security event."""


@dataclass(frozen=True)
class Trigger:
    part: str
    store_number: str
    seen_at: datetime
    nonce: str

    def age(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(timezone.utc)) - self.seen_at).total_seconds()


# ── signing ───────────────────────────────────────────────────────────────────
def canonical(payload: dict) -> bytes:
    """The bytes that get signed: the payload without ``sig``, keys sorted, no spaces."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(payload: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(payload), hashlib.sha256).hexdigest()


def make(part: str, store_number: str, secret: str, *, seen_at: datetime | None = None,
         nonce: str | None = None) -> dict:
    """Build a signed trigger payload (what a detector publishes)."""
    seen = (seen_at or datetime.now(timezone.utc).astimezone()).isoformat(timespec="seconds")
    payload = {"v": VERSION, "part": part, "store_number": store_number, "seen_at": seen,
               "nonce": nonce or secrets.token_hex(16)}
    payload["sig"] = sign(payload, secret)
    return payload


# ── replay protection ─────────────────────────────────────────────────────────
class NonceStore:
    """Nonces already acted on, persisted so a restart can't be used to replay."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self._seen: dict[str, float] = {}
        if self.path.exists():
            try:
                self._seen = {str(k): float(v) for k, v in json.loads(self.path.read_text()).items()}
            except (ValueError, AttributeError):
                log.warning("nonce store %s unreadable; starting empty", self.path)

    def __contains__(self, nonce: str) -> bool:
        return nonce in self._seen

    def add(self, nonce: str) -> None:
        now = self.clock()
        self._seen = {n: t for n, t in self._seen.items() if now - t < NONCE_KEEP_SECONDS}
        self._seen[nonce] = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._seen))
        tmp.replace(self.path)


# ── validation ────────────────────────────────────────────────────────────────
def validate(raw: str, *, secret: str, parts: set[str], store_allowed: Callable[[str], bool],
             max_age_seconds: int, nonces: NonceStore, now: datetime | None = None) -> Trigger:
    """Parse and check one trigger message. Raises TriggerRejected; records the nonce on success."""
    if not secret:
        raise TriggerRejected("no trigger secret configured")
    try:
        p = json.loads(raw)
    except (ValueError, TypeError):
        raise TriggerRejected("not JSON")
    if not isinstance(p, dict):
        raise TriggerRejected("not a JSON object")

    # Signature first: nothing else in an unsigned message is worth believing.
    sig = p.get("sig")
    if not isinstance(sig, str) or not sig:
        raise TriggerRejected("unsigned")
    if not hmac.compare_digest(sig.lower().encode("utf-8"), sign(p, secret).encode("utf-8")):
        raise TriggerRejected("bad signature")

    if p.get("v") != VERSION:
        raise TriggerRejected(f"unsupported version {p.get('v')!r}")
    part, store, nonce = p.get("part"), p.get("store_number"), p.get("nonce")
    if not (isinstance(part, str) and isinstance(store, str) and isinstance(nonce, str) and nonce):
        raise TriggerRejected("missing part, store_number or nonce")
    if part not in parts:
        raise TriggerRejected(f"part {part} is not a watched model")
    if not store_allowed(store):
        raise TriggerRejected(f"store {store} is outside the configured store filter")

    try:
        seen = datetime.fromisoformat(str(p.get("seen_at")))
    except ValueError:
        raise TriggerRejected("bad seen_at")
    if seen.tzinfo is None:
        raise TriggerRejected("seen_at has no timezone")
    age = ((now or datetime.now(timezone.utc)) - seen).total_seconds()
    if age < -FUTURE_SKEW_SECONDS:
        raise TriggerRejected(f"seen_at is {-age:.0f}s in the future")
    if age > max_age_seconds:
        raise TriggerRejected(f"expired ({age:.0f}s old, max {max_age_seconds}s)")

    if nonce in nonces:
        raise TriggerRejected("replayed nonce")
    nonces.add(nonce)
    return Trigger(part=part, store_number=store, seen_at=seen, nonce=nonce)


# ── ntfy subscription ─────────────────────────────────────────────────────────
@dataclass
class Event:
    kind: str          # "connected" | "keepalive" | "message" | "disconnected"
    text: str = ""     # message body, or the disconnect reason
    id: str = ""


def listen(server: str, topic: str, *, token: str = "", backoff_s: float = 5, max_backoff_s: float = 300,
           http_get=None, sleep: Callable[[float], None] = time.sleep,
           read_timeout_s: float = 90) -> Iterator[Event]:
    """Subscribe to an ntfy topic forever, reconnecting with doubling backoff.

    ntfy sends a keepalive at least every 45s, so a read timeout of 90s means a
    silently dead connection is noticed and replaced. After a reconnect it asks
    for messages ``since`` the last one seen, so nothing published during the
    gap is lost (stale ones are rejected by ``max_age_seconds`` anyway).
    """
    http_get = http_get or requests.get
    url = f"{server.rstrip('/')}/{topic}/json"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last_id = ""
    delay = backoff_s
    while True:
        params = {"since": last_id} if last_id else {}
        try:
            with http_get(url, params=params, headers=headers, stream=True,
                          timeout=(10, read_timeout_s)) as r:
                if r.status_code in (401, 403):
                    raise PermissionError(f"ntfy refused the subscription (HTTP {r.status_code}) — check trigger.token")
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    kind = ev.get("event")
                    if kind == "open":
                        delay = backoff_s  # connected: reset the backoff
                        yield Event("connected")
                    elif kind == "keepalive":
                        yield Event("keepalive")
                    elif kind == "message":
                        last_id = ev.get("id") or last_id
                        yield Event("message", str(ev.get("message") or ""), last_id)
            reason = "subscription closed by server"
        except PermissionError:
            raise
        except Exception as e:  # network blip, timeout, 5xx: reconnect
            reason = f"{type(e).__name__}: {e}"
        yield Event("disconnected", reason)
        sleep(delay)
        delay = min(delay * 2, max_backoff_s)


def publish(server: str, topic: str, payload: dict, *, token: str = "", http_post=None) -> None:
    """Publish a signed trigger (for detectors and `./duo trigger-send`)."""
    http_post = http_post or requests.post
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http_post(f"{server.rstrip('/')}/{topic}", data=json.dumps(payload).encode("utf-8"),
                  headers=headers, timeout=10)
    r.raise_for_status()
