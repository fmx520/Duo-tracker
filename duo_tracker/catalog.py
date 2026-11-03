"""US iPhone Duo models, as listed on apple.com/shop/buy-iphone/iphone-duo (Nov 2026).

Each model is keyed "<capacity>-<color>" so config.yaml stays readable. The
capacity/color strings are the exact ``value`` attributes of the radio inputs
on Apple's buy page (``input[name=dimensionCapacity]`` / ``dimensionColor``).
"""
from __future__ import annotations

from dataclasses import dataclass

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
BAG_URL = "https://www.apple.com/shop/bag"


@dataclass(frozen=True)
class Model:
    key: str
    part: str
    capacity: str
    color: str
    name: str
    price: int


MODELS: dict[str, Model] = {
    m.key: m
    for m in [
        Model("256gb-nightsky", "MK1F4LL/A", "256gb", "nightsky", "iPhone Duo 256GB Night Sky", 1999),
        Model("256gb-starwhite", "MK1E4LL/A", "256gb", "starwhite", "iPhone Duo 256GB Star White", 1999),
        Model("512gb-nightsky", "MK1H4LL/A", "512gb", "nightsky", "iPhone Duo 512GB Night Sky", 2199),
        Model("512gb-starwhite", "MK1G4LL/A", "512gb", "starwhite", "iPhone Duo 512GB Star White", 2199),
        Model("1tb-nightsky", "MK1L4LL/A", "1tb", "nightsky", "iPhone Duo 1TB Night Sky", 2599),
        Model("1tb-starwhite", "MK1J4LL/A", "1tb", "starwhite", "iPhone Duo 1TB Star White", 2599),
        Model("2tb-nightsky", "MK1R4LL/A", "2tb", "nightsky", "iPhone Duo 2TB Night Sky", 3199),
        Model("2tb-starwhite", "MK1Q4LL/A", "2tb", "starwhite", "iPhone Duo 2TB Star White", 3199),
    ]
}

# Stores worth watching from a ZIP, with Apple store numbers, ranked by how
# often the Duo dropped there Oct 26 – Nov 2 (public drop history). Names and
# numbers both go in the store filter so either spelling matches.
ZIP_PRESETS = {
    "11754": {  # Kings Park, NY
        "stores": ["Smith Haven", "R139", "Walt Whitman", "R068", "Roosevelt Field", "R060", "Manhasset", "R327"],
        "models": ["256gb-nightsky", "256gb-starwhite", "512gb-nightsky", "512gb-starwhite"],
        "max_miles": 30,
    },
}

# Values of input[name=carrierModel] on the buy page.
CARRIERS = {
    "unlocked": "UNLOCKED/US",
    "att": "ATT_IPHONEDUO",
    "tmobile": "TMOBILE_IPHONEDUO",
    "verizon": "VERIZON_IPHONEDUO",
}


def resolve(key: str) -> Model:
    k = key.strip().lower().replace(" ", "")
    if k in MODELS:
        return MODELS[k]
    for m in MODELS.values():
        if m.part.lower() == k:
            return m
    raise KeyError(f"unknown model {key!r}; choose from: {', '.join(MODELS)}")
