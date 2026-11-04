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


def parse_iso(value: str) -> datetime:
    """``datetime.fromisoformat`` that also takes a trailing ``Z``.

    JavaScript's ``toISOString()`` writes ``…123Z``. Python 3.11+ parses that,
    but 3.10 (Ubuntu 22.04's Python) raises ValueError, which would reject
    every trigger from a JS publisher on the Linux box.
    """
    v = str(value).strip()
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)

log = logging.getLogger(__name__)

VERSION = 1
# Detector and bot clocks are both NTP-synced, but allow a little drift before
# calling a timestamp "from the future".
FUTURE_SKEW_SECONDS = 5
NONCE_KEEP_SECONDS = 24 * 3600


class TriggerRejected(Exception):
    """A trigger failed validation.

    ``security`` is True unless the trigger was correctly signed and fresh but
    simply not wanted (a model or store you don't watch) -- that is your own
    detector being broader than your filter, not an attack.
    """

    def __init__(self, reason: str, security: bool = True):
        super().__init__(reason)
        self.security = security


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
    try:
        seen = parse_iso(p.get("seen_at"))
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
    # Signed, fresh and new, so it came from our detector: an unwatched model or
    # store is a filter mismatch, not a security event.
    if part not in parts:
        raise TriggerRejected(f"part {part} is not a watched model", security=False)
    if not store_allowed(store):
        raise TriggerRejected(f"store {store} is outside the configured store filter", security=False)
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


# ── radar source (duo-tracker-six.vercel.app) ─────────────────────────────────
DEFAULT_RADAR_URL = "https://duo-tracker-six.vercel.app"
RADAR_STALE_SECONDS = 60      # radar's own last good sweep older than this → treat as down
RADAR_REEMIT_SECONDS = 30     # same (store, part) won't re-trigger within this window


def radar(url: str, *, secret: str, parts: set[str], store_allowed: Callable[[str], bool],
          poll_s: float = 5, http_get=None, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] | None = None,
          max_backoff_s: float = 120) -> Iterator[Event]:
    """Poll the public NYC pickup radar and turn new in-stock entries into signed triggers.

    The radar is someone else's unauthenticated service, so its data is only a
    hint: each entry becomes a locally signed trigger that then goes through
    the same validate() → confirm-with-Apple path as any other. ``seen_at`` is
    the radar's own ``changed_at``, so the 90s freshness limit measures from
    when the radar saw the drop, not from when we noticed.

    Yields "keepalive" after each healthy poll, "disconnected" when the radar is
    unreachable or its last sweep is stale, and "message" for each new hit.
    """
    import random

    http_get = http_get or requests.get
    now = now or (lambda: datetime.now(timezone.utc))
    endpoint = f"{url.rstrip('/')}/api/state"
    headers = {"User-Agent": "Duo-tracker trigger bridge (personal use)", "Cache-Control": "no-cache"}
    seen: set[tuple] = set()            # (store, part, changed_at) already emitted
    last_emit: dict[tuple, float] = {}  # (store, part) -> clock() of last emit
    delay = poll_s
    while True:
        try:
            r = http_get(endpoint, headers=headers, timeout=5)
            r.raise_for_status()
            data = r.json()
            status = data.get("status") or {}
            ok_at = status.get("last_ok_at")
            age = (now() - parse_iso(ok_at)).total_seconds() if ok_at else None
            if age is None or age > RADAR_STALE_SECONDS:
                yield Event("disconnected", f"radar stale (last good sweep {'never' if age is None else f'{age:.0f}s ago'})")
            else:
                delay = poll_s
                yield Event("keepalive")
                for entry in data.get("stock") or []:
                    store, part, changed = entry.get("store_id"), entry.get("part_number"), entry.get("changed_at")
                    if not (isinstance(store, str) and isinstance(part, str) and isinstance(changed, str)):
                        continue
                    if part not in parts or not store_allowed(store):
                        continue
                    if (store, part, changed) in seen:
                        continue
                    seen.add((store, part, changed))
                    if clock() - last_emit.get((store, part), -1e9) < RADAR_REEMIT_SECONDS:
                        continue
                    last_emit[(store, part)] = clock()
                    try:
                        seen_at = parse_iso(changed)
                    except ValueError:
                        continue
                    yield Event("message", json.dumps(make(part, store, secret, seen_at=seen_at)), f"radar:{store}:{part}")
                if len(seen) > 5000:
                    seen = set(list(seen)[-1000:])
        except Exception as e:
            yield Event("disconnected", f"radar unreachable: {type(e).__name__}: {e}")
            delay = min(max(delay, poll_s) * 2, max_backoff_s)
        sleep(delay * random.uniform(0.85, 1.15))


def merged(sources: dict[str, Callable[[], Iterator[Event]]]) -> Iterator[tuple[str, Event]]:
    """Run each event source in its own thread; yield (source_name, event) as they arrive.

    Sources only do network I/O; everything that touches the browser stays on
    the caller's thread. A source that raises is reported once as a "fatal"
    event and not restarted; one that simply finishes reports "ended". The
    iterator stops when every source has done one or the other.
    """
    import queue
    import threading

    q: queue.Queue = queue.Queue()

    def pump(name, factory):
        try:
            for ev in factory():
                q.put((name, ev))
            q.put((name, Event("ended")))
        except BaseException as e:  # noqa: BLE001 - report, don't die silently
            q.put((name, Event("fatal", f"{type(e).__name__}: {e}")))

    for name, factory in sources.items():
        threading.Thread(target=pump, args=(name, factory), daemon=True, name=f"trigger-{name}").start()
    alive = len(sources)
    while alive:
        name, ev = q.get()
        if ev.kind in ("fatal", "ended"):
            alive -= 1
        yield name, ev
