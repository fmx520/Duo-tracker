"""`./duo report`: one file with everything needed to diagnose a problem.

Personal details are scrubbed before anything is written: email addresses,
phone numbers, card-like digit runs, street addresses, Apple order numbers,
and your ntfy topic / healthcheck URL (anyone with those could read or fake
your alerts). Skim the file before sending it anyway.
"""
from __future__ import annotations

import json
import platform
import re
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .drops import summarize as summarize_drops
from .logsetup import log_path
from .schedule import summarize_blocks

LOG_LINES = 600
MAX_REPORTS = 3

_SCRUBBERS = [
    # Any credential-ish config value: trigger.secret, tokens, passwords, API keys.
    (re.compile(r"(?im)^(\s*[\w.-]*(?:secret|token|password|passwd|api[_-]?key)[\w.-]*\s*[:=]\s*)"
                r"(['\"]?)(?!\2\s*$)[^\s'\"#]+\2"), r"\1\2<secret>\2"),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "<hex-secret>"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "<email>"),
    (re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"), "<phone>"),
    (re.compile(r"\b\d{1,6}\s+(?:[A-Z][\w'.-]*\s+){1,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|"
                r"Dr|Drive|Ct|Court|Pl|Place|Way|Ter|Terrace|Pkwy|Parkway|Hwy|Highway|Cir|Circle)\b\.?", re.I),
     "<street-address>"),
    (re.compile(r"\bW\d{9,10}\b"), "<order-number>"),
    (re.compile(r"(?i)(ntfy\.sh/|topic:\s*['\"]?)[\w-]{4,}"), r"\1<ntfy-topic>"),
    (re.compile(r"(?i)(hc-ping\.com/|healthcheck_url:\s*['\"]?https?://\S*?/)[\w-]{6,}"), r"\1<healthcheck-id>"),
]


# Card numbers: 13–19 digits written solid or in groups of 4, AND passing the
# Luhn checksum, so timestamps and file names aren't mistaken for cards.
_CARD = re.compile(r"(?<![\d-])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7})(?![\d-])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "<card-number>" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def scrub(text: str, extra_secrets: list[str] = ()) -> str:
    for s in extra_secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "<secret>")
    text = _CARD.sub(_card, text)
    for rx, repl in _SCRUBBERS:
        text = rx.sub(repl, text)
    return text


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except FileNotFoundError:
        return []


def _section(title: str, body: str) -> str:
    return f"\n===== {title} =====\n{body.rstrip()}\n"


def build(state_dir: Path, config_path: Path) -> Path:
    secrets: list[str] = []
    cfg_text = "(no config file)"
    if config_path.exists():
        cfg_text = config_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"topic:\s*['\"]?([\w-]+)", cfg_text)
        if m:
            secrets.append(m.group(1))
        m = re.search(r"healthcheck_url:\s*['\"]?(\S+?)['\"]?\s*$", cfg_text, re.M)
        if m and m.group(1) not in ('""', "''"):
            secrets.append(m.group(1))

    try:
        from importlib.metadata import version
        pw_version = version("playwright")
    except Exception:
        pw_version = "not installed"

    parts = [
        f"Duo-tracker support report — {datetime.now().isoformat(timespec='seconds')}",
        f"version {__version__} · Python {sys.version.split()[0]} · Playwright {pw_version} · "
        f"{platform.system()} {platform.release()} ({platform.machine()})",
        _section("config.yaml", cfg_text),
    ]

    for name in ("rehearsal.json", "order.lock"):
        p = state_dir / name
        parts.append(_section(name, p.read_text(encoding="utf-8") if p.exists() else "(none)"))

    parts.append(_section("Apple blocks", summarize_blocks(state_dir / "blocks.csv")))
    parts.append(_section("Drops (last 7 days)", summarize_drops(state_dir / "drops.jsonl")))

    reports = sorted((state_dir / "reports").glob("*.json"))[-MAX_REPORTS:] if (state_dir / "reports").exists() else []
    for r in reports:
        try:
            body = json.dumps(json.loads(r.read_text(encoding="utf-8")), indent=1)
        except Exception:
            body = r.read_text(encoding="utf-8", errors="replace")
        parts.append(_section(f"checkout report {r.name}", body))
    if not reports:
        parts.append(_section("checkout reports", "(none)"))

    log = log_path(state_dir)
    lines = _tail(log, LOG_LINES)
    if len(lines) < LOG_LINES:  # top up from yesterday's rotated file
        older = sorted(log.parent.glob(log.name + ".*"))
        if older:
            lines = _tail(older[-1], LOG_LINES - len(lines)) + lines
    parts.append(_section(f"log (last {len(lines)} lines)", "\n".join(lines) or "(empty)"))

    out_dir = state_dir / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"duo-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
    out.write_text(scrub("\n".join(parts), secrets), encoding="utf-8")
    return out
