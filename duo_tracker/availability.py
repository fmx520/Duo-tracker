"""Parse the JSON Apple's buy page receives from /shop/fulfillment-messages.

We never call that endpoint ourselves: the watcher captures the response the
page fetched on its own when a configuration was selected. Parsing is
defensive because Apple changes this payload without notice.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StoreStock:
    store_name: str
    store_number: str
    city: str
    distance: str
    available: bool
    quote: str


@dataclass
class Availability:
    part: str
    stores: list[StoreStock] = field(default_factory=list)
    delivery_quote: str = ""
    has_pickup_data: bool = False

    @property
    def in_stock_stores(self) -> list[StoreStock]:
        return [s for s in self.stores if s.available]


def _first_str(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _delivery_quote(dm: dict, part: str) -> str:
    entry = dm.get(part) if isinstance(dm, dict) else None
    if not isinstance(entry, dict):
        return ""
    for mt in ("regular", "sticky", "compact"):
        block = entry.get(mt)
        if not isinstance(block, dict):
            continue
        for opt in block.get("deliveryOptionMessages") or []:
            if isinstance(opt, dict) and opt.get("displayName"):
                return str(opt["displayName"]).strip()
        q = _first_str(block.get("deliveryOptionsShortMessage"), block.get("deliveryQuote"))
        if q:
            return q
    return ""


def parse(payload: dict, part: str) -> Availability:
    content = (payload or {}).get("body", {}).get("content", {}) or {}
    result = Availability(part=part)

    pickup = content.get("pickupMessage") or {}
    stores = pickup.get("stores")
    if isinstance(stores, list):
        result.has_pickup_data = True
        for s in stores:
            pa = (s.get("partsAvailability") or {}).get(part) or {}
            display = str(pa.get("pickupDisplay", "")).lower()
            result.stores.append(
                StoreStock(
                    store_name=_first_str(s.get("storeName"), s.get("address", {}).get("address")),
                    store_number=str(s.get("storeNumber", "")),
                    city=_first_str(s.get("city")),
                    distance=_first_str(s.get("storedistanceWithUnit"), s.get("storeDistanceWithUnit")),
                    available=display == "available",
                    quote=_first_str(pa.get("pickupSearchQuote"), pa.get("storePickupQuote")),
                )
            )

    result.delivery_quote = _delivery_quote(content.get("deliveryMessage") or {}, part)
    return result


def matching_stores(av: Availability, store_filter: list[str]) -> list[StoreStock]:
    """In-stock stores, narrowed to ones matching any filter string (name, city, or number)."""
    stores = av.in_stock_stores
    if store_filter:
        stores = [s for s in stores
                  if any(f in f"{s.store_name} {s.city} {s.store_number}".lower() for f in store_filter)]
    return stores
