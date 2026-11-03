# Trigger mode — design spec

Status: **built** (`duo_tracker/trigger.py`, `cli._watch_triggers`, tests in
`tests/test_trigger.py` cover acceptance criteria 1–7). The external detector
is still out of scope. Written 2026-11-04.

Implementation notes: `trigger.action` (`submit`|`review`) picks what a
confirmed trigger does; `submit` still needs a passing rehearsal or it falls
back to `review`. `seen_at` may be up to 5s in the future (clock skew).
Rejected-trigger pushes are rate-limited to one per 5 minutes so a flood
can't bury real alerts. On reconnect the listener asks ntfy for messages
`since` the last id, so a blip doesn't lose a trigger.

## What it is

A fourth source of truth for "a Duo just dropped". Today the watcher finds
stock itself by polling; the ntfy push is a one-way side effect that tells the
owner and reaches nothing else. Trigger mode inverts that: an **external**
detector says a drop happened, the bot confirms it, and checkout runs.

```
today     poll Apple every 30-90s ──→ detect ──┬──→ ntfy push ──→ owner's phone (one way)
                                               └──→ _act_on_hit() ──→ bag → checkout

proposed  external detector ──→ signed trigger ──→ bot wakes ──→ ONE confirming
                                                   availability check ──→ _act_on_hit()
```

**Why it's worth building:** polling is essentially all of the tool's Apple
traffic. Under trigger mode the bot makes roughly one request per real drop
instead of one every 30-90 seconds all day. Nothing else in the project
changes the traffic profile by that much.

## Security: the part that is not optional

An ntfy.sh topic is readable **and writable** by anyone who knows its name.
Today that is tolerable because the topic is only an outbound mailbox. The
moment a message can cause a purchase, **the topic name becomes a remote
button that spends $2,199.** The current alert topic has been shown on screen,
pasted into chat, and typed into a phone app.

Requirements, all of them:

- **A separate inbound topic.** Never reuse the alert topic.
- **Authentication.** Either an ntfy access token on a reserved topic, or an
  HMAC-SHA256 signature over the payload using a shared secret from config.
  Prefer both. An unauthenticated trigger path must not be merged.
- **Replay protection.** Payload carries `seen_at`; reject anything older than
  `trigger.max_age_seconds` (default 90 — a Long Island drop has a ~73s median
  life, so anything older is useless anyway).
- **Deduplication.** Payload carries a `nonce`; act at most once per nonce.
  Keep recent nonces in `~/.duo-tracker/`.
- **Fail closed.** Any validation failure: do not act, log it, and push an
  alert. A rejected trigger is a security event, not noise.

## Payload

```json
{
  "v": 1,
  "part": "MK1F4LL/A",
  "store_number": "R068",
  "seen_at": "2026-11-04T08:12:31-05:00",
  "nonce": "9f2c1a...",
  "sig": "<hex HMAC-SHA256 of the canonical JSON without sig, keyed by trigger.secret>"
}
```

Reject: wrong `v`, unknown `part`, a `store_number` outside the configured
store filter, missing or bad `sig`, `seen_at` in the future or older than
`max_age_seconds`, a seen `nonce`.

## The bot still verifies

**A trigger is a hint, never an instruction.** On a valid trigger the bot must
still run its own availability check for that part and confirm the named store
is genuinely in stock before buying anything.

- Trigger and reality agree → proceed into the existing `_act_on_hit()`.
- They disagree → push "trigger did not match stock", log it, do not buy.

Everything downstream is unchanged and must stay that way: nearest in-stock
store selection, the review-page model/store guard, the total cap, the
sign-in/2FA and card-entry stops, and the one-time `~/.duo-tracker/order.lock`.
Trigger mode changes **what wakes the bot**, nothing about what it is allowed
to do once awake.

## Config

```yaml
checkout:
  mode: trigger          # alert | bag | review | submit | trigger

trigger:
  server: https://ntfy.sh
  topic: ""              # MUST differ from ntfy.topic
  secret: ""             # shared HMAC secret; refuse to start if empty
  max_age_seconds: 90
  reconnect_backoff_seconds: 5   # doubling, capped at 300
```

Validation at load: refuse to start in `trigger` mode when `topic` is empty,
`secret` is empty, or `topic == ntfy.topic`.

## Health, which needs rethinking

This is the subtle part. `Health` currently equates "working" with "a check
succeeded recently" — `success()` feeds the stall detector and pings the
dead-man switch. **In trigger mode there are no periodic checks**, so as
written the watcher would report itself stalled within 10 minutes and the
healthchecks.io check would flip to down on a perfectly healthy bot.

The listener needs its own liveness signal:

- Treat "subscription connected" as the health heartbeat; ping the dead-man
  switch on a timer while connected rather than on check success.
- Alert when the subscription drops and cannot reconnect within
  `stall_minutes`.
- Reconnect with the configured backoff; never spin.
- The daily check-in should report triggers received and rejected, not checks.

## Latency budget

Walt Whitman's median drop life is 73 seconds. The chain is: detector notices →
detector publishes → ntfy delivers → bot wakes → bot confirms → checkout runs.
Checkout needs most of that window on its own, so the detector's own lag is the
deciding factor. Measure it before trusting it — if the external detector is
20+ seconds behind the drop, trigger mode will reliably lose.

Keep `review`/`submit` polling available as a fallback for exactly this reason.

## Acceptance criteria

A valid, signed, fresh trigger for a watched part at a filtered store, where a
confirming check agrees, results in exactly one `_act_on_hit()` call. Tests
must cover, with no network:

1. Unsigned, wrong-signature, expired, future-dated, and replayed nonce
   triggers are all rejected and nothing is bought.
2. An unknown part or an unfiltered store is rejected.
3. A valid trigger whose confirming check shows no stock does not buy, and
   alerts.
4. A valid confirmed trigger buys once; a duplicate nonce after it buys nothing.
5. `order.lock` still blocks a second order.
6. Config validation refuses an empty secret, an empty topic, or a topic equal
   to the alert topic.
7. The listener reconnects after a dropped connection, and the dead-man switch
   keeps being pinged while connected.

## Out of scope

Choosing the external detector. The spec assumes something publishes signed
triggers; which service, and whether its latency is good enough, is a separate
decision (see "Latency budget").
