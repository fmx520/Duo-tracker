"""Parse the JSON Apple's buy page receives from /shop/fulfillment-messages.

We never call that endpoint ourselves: the watcher captures the response the
page fetched on its own when a configuration was selected. Parsing is
defensive because Apple changes this payload without notice. Field names
cross-checked against kelindi/apple-stock's parser (MIT).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class StoreStock:
    store_name: str
    store_number: str
    city: str
    distance: str
    miles: float | None
    available: bool
    quote: str


@dataclass
class Availability:
    part: str
    stores: list[StoreStock] = field(default_factory=list)
    delivery_quote: str = ""
    courier_available: bool = False
    courier_quote: str = ""
    has_pickup_data: bool = False

    @property
    def in_stock_stores(self) -> list[StoreStock]:
        return [s for s in self.stores if s.available]


def _first_str(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


_KM_PER_MILE = 1.609344


def _miles(text: str) -> float | None:
    m = re.search(r"([\d.,]+)\s*(mi|km)\b", text or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n if m.group(2) == "mi" else n / _KM_PER_MILE


def _delivery(result: Availability, dm: dict, part: str) -> None:
    entry = dm.get(part) if isinstance(dm, dict) else None
    if not isinstance(entry, dict):
        return
    block = entry.get("regular") or entry.get("sticky") or entry.get("compact") or {}
    if not isinstance(block, dict):
        return
    opts = [o for o in block.get("deliveryOptionMessages") or [] if isinstance(o, dict)]
    result.delivery_quote = _first_str(
        block.get("stickyMessageSTH"),
        opts[0].get("displayName") if opts else None,
        block.get("deliveryOptionsShortMessage"),
    )
    result.courier_quote = _first_str(block.get("stickyMessageIDL"))
    result.courier_available = block.get("idl") is True and not re.search(
        r"unavailable", result.courier_quote, re.I
    )


def parse(payload: dict, part: str) -> Availability:
    content = (payload or {}).get("body", {}).get("content", {}) or {}
    result = Availability(part=part)

    pickup = content.get("pickupMessage") or {}
    stores = pickup.get("stores")
    if isinstance(stores, list):
        result.has_pickup_data = True
        for s in stores:
            if not isinstance(s, dict):
                continue
            pa = (s.get("partsAvailability") or {}).get(part) or {}
            regular = (pa.get("messageTypes") or {}).get("regular") or {}
            dist = _first_str(s.get("storeDistanceWithUnit"), s.get("storedistanceWithUnit"))
            result.stores.append(
                StoreStock(
                    store_name=_first_str(s.get("storeName")),
                    store_number=str(s.get("storeNumber", "")),
                    city=_first_str(s.get("city")),
                    distance=dist,
                    miles=_miles(dist),
                    available=str(pa.get("pickupDisplay", "")).lower() == "available",
                    quote=_first_str(pa.get("pickupSearchQuote"), regular.get("storePickupQuote")),
                )
            )

    _delivery(result, content.get("deliveryMessage") or {}, part)
    return result


def matching_stores(av: Availability, store_filter: list[str], max_miles: float | None = None) -> list[StoreStock]:
    """In-stock stores, narrowed by name/city/number filter and distance."""
    stores = av.in_stock_stores
    if store_filter:
        stores = [s for s in stores
                  if any(f in f"{s.store_name} {s.city} {s.store_number}".lower() for f in store_filter)]
    if max_miles is not None:
        stores = [s for s in stores if s.miles is None or s.miles <= max_miles]
    return stores


def hit_keys(part: str, av: Availability, stores: list[StoreStock], courier: bool) -> dict[str, str]:
    """Stable keys for what's in stock right now -> human description.

    The watcher alerts only on keys that weren't present last round, so a
    model sitting in stock doesn't re-alert, but a fresh drop does.
    """
    keys = {f"pickup:{s.store_number}:{part}": f"{s.store_name}" + (f" ({s.distance})" if s.distance else "")
            for s in stores}
    if courier and av.courier_available:
        keys[f"courier:{part}"] = f"courier delivery — {av.courier_quote or 'available'}"
    return keys
