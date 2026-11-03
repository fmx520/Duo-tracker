"""History of every drop the watcher saw and what it did about it.

Append-only JSON lines in ~/.duo-tracker/drops.jsonl (robust to crashes):
  {"type": "start",   "id", "at", "model", "part", "store", "store_number", "kind", "notice_gap_s", "window"}
  {"type": "outcome", "id", "at", "outcome", "detail", "checkout_s"}
  {"type": "end",     "id", "at"}

notice_gap_s = time since the previous check of that model, i.e. the drop
appeared at most this long before we noticed it. Seen duration is from our
first to our last sighting, so it's a lower bound on how long it lasted.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

OUTCOMES = ("ordered", "review ready", "needs you", "bagged", "add-to-bag failed", "alert only")


class DropLog:
    def __init__(self, path: Path, now=datetime.now):
        self.path, self.now = path, now

    def _write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def start(self, *, key: str, model: str, part: str, store: str, store_number: str, kind: str,
              notice_gap_s: float | None, window: str) -> str:
        at = self.now().isoformat(timespec="seconds")
        drop_id = f"{key}@{at}"
        self._write({"type": "start", "id": drop_id, "at": at, "model": model, "part": part, "store": store,
                     "store_number": store_number, "kind": kind,
                     "notice_gap_s": None if notice_gap_s is None else round(notice_gap_s, 1), "window": window})
        return drop_id

    def outcome(self, drop_id: str, outcome: str, detail: str = "", checkout_s: float | None = None) -> None:
        self._write({"type": "outcome", "id": drop_id, "at": self.now().isoformat(timespec="seconds"),
                     "outcome": outcome, "detail": detail,
                     "checkout_s": None if checkout_s is None else round(checkout_s, 1)})

    def end(self, drop_id: str) -> None:
        self._write({"type": "end", "id": drop_id, "at": self.now().isoformat(timespec="seconds")})


def load(path: Path) -> list[dict]:
    drops: dict[str, dict] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        d = drops.setdefault(rec["id"], {"id": rec["id"]})
        if rec["type"] == "start":
            d.update({k: v for k, v in rec.items() if k not in ("type", "at")}, first_seen=rec["at"])
        elif rec["type"] == "outcome":
            d.update(outcome=rec["outcome"], detail=rec.get("detail", ""), checkout_s=rec.get("checkout_s"))
        elif rec["type"] == "end":
            d["last_seen"] = rec["at"]
    out = []
    for d in drops.values():
        if "first_seen" not in d:
            continue
        if d.get("last_seen"):
            d["seen_s"] = (datetime.fromisoformat(d["last_seen"]) - datetime.fromisoformat(d["first_seen"])).total_seconds()
        out.append(d)
    return sorted(out, key=lambda d: d["first_seen"])


def _median(xs):
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):.0f}s" if xs else "–"


def summarize(path: Path, days: int = 7, now=datetime.now) -> str:
    cutoff = now() - timedelta(days=days)
    drops = [d for d in load(path) if datetime.fromisoformat(d["first_seen"]) >= cutoff]
    if not drops:
        return f"No drops seen in the last {days} days."
    outcomes = Counter((d.get("outcome") or "still open / unknown") for d in drops)
    lines = [
        f"{len(drops)} drops seen in the last {days} days.",
        "  outcomes: " + ", ".join(f"{k} {v}" for k, v in outcomes.most_common()),
        f"  noticed within (median): {_median(d.get('notice_gap_s') for d in drops)}"
        f"   checkout took (median): {_median(d.get('checkout_s') for d in drops)}"
        f"   seen for at least (median): {_median(d.get('seen_s') for d in drops)}",
        "  by store: " + ", ".join(f"{k} {v}" for k, v in Counter(d["store"] for d in drops).most_common()),
        "  by model: " + ", ".join(f"{k} {v}" for k, v in Counter(d["model"] for d in drops).most_common()),
        "", "Most recent:",
    ]
    for d in drops[-10:]:
        seen = f"{d['seen_s']:.0f}s" if d.get("seen_s") is not None else "?"
        gap = f"≤{d['notice_gap_s']:.0f}s" if d.get("notice_gap_s") is not None else "?"
        what = d.get("outcome") or "–"
        if d.get("detail"):
            what += f" ({d['detail']})"
        lines.append(f"  {d['first_seen'].replace('T', ' ')}  {d['model']:<28} {d['store']:<18} "
                     f"noticed {gap:<6} seen {seen:<5} → {what}")
    return "\n".join(lines)


def export_csv(path: Path, out: Path) -> int:
    drops = load(path)
    cols = ["first_seen", "last_seen", "seen_s", "model", "part", "store", "store_number", "kind",
            "window", "notice_gap_s", "outcome", "detail", "checkout_s"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(drops)
    return len(drops)
