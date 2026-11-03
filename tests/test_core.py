import textwrap

import pytest

from duo_tracker.availability import matching_stores, parse
from duo_tracker.catalog import MODELS, resolve
from duo_tracker.config import load

PART = "MK1F4LL/A"


def payload(displays):
    return {
        "body": {"content": {
            "pickupMessage": {"stores": [
                {"storeName": name, "storeNumber": f"R{i:03}", "city": city, "storedistanceWithUnit": "1.0 mi",
                 "partsAvailability": {PART: {"pickupDisplay": disp, "pickupSearchQuote": "Available Today" if disp == "available" else "Unavailable for Pickup"}}}
                for i, (name, city, disp) in enumerate(displays)
            ]},
            "deliveryMessage": {PART: {"regular": {"deliveryOptionMessages": [{"displayName": "Delivers Jan 25 – Feb 1"}]}}},
        }}
    }


def test_parse_in_stock_and_delivery():
    av = parse(payload([("Fifth Avenue", "New York", "available"), ("SoHo", "New York", "unavailable")]), PART)
    assert av.has_pickup_data
    assert [s.store_name for s in av.in_stock_stores] == ["Fifth Avenue"]
    assert av.in_stock_stores[0].quote == "Available Today"
    assert av.delivery_quote.startswith("Delivers Jan 25")


def test_parse_missing_store_data_and_junk():
    av = parse({"body": {"content": {}}}, PART)
    assert not av.has_pickup_data and av.stores == [] and av.delivery_quote == ""
    assert parse({}, PART).stores == []
    # store without this part listed counts as unavailable, not a crash
    av = parse({"body": {"content": {"pickupMessage": {"stores": [{"storeName": "X", "partsAvailability": {}}]}}}}, PART)
    assert av.has_pickup_data and av.in_stock_stores == []


def test_store_filter():
    av = parse(payload([("Fifth Avenue", "New York", "available"), ("Roosevelt Field", "Garden City", "available")]), PART)
    assert len(matching_stores(av, [])) == 2
    assert [s.store_name for s in matching_stores(av, ["garden city"])] == ["Roosevelt Field"]
    assert [s.store_name for s in matching_stores(av, ["r000"])] == ["Fifth Avenue"]


def test_catalog_resolve():
    assert len(MODELS) == 8
    assert resolve("256GB-NightSky").part == "MK1F4LL/A"
    assert resolve("mk1q4ll/a").key == "2tb-starwhite"
    with pytest.raises(KeyError):
        resolve("128gb-blue")


def write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_config_ok(tmp_path):
    cfg = load(write(tmp_path, """
        models: [256gb-nightsky, 1tb-starwhite]
        carrier: verizon
        stores: [Fifth Avenue]
        ntfy: {topic: abc}
        checkout: {mode: checkout}
    """))
    assert [m.part for m in cfg.models] == ["MK1F4LL/A", "MK1J4LL/A"]
    assert cfg.carrier == "VERIZON_IPHONEDUO"
    assert cfg.store_filter == ["fifth avenue"]
    assert cfg.interval_seconds == 30 and cfg.checkout_mode == "review"  # legacy name maps to review


@pytest.mark.parametrize("body,msg", [
    ("models: []", "at least one model"),
    ("models: [256gb-nightsky]\ninterval_seconds: 5", ">= 30"),
    ("models: [256gb-nightsky]\ncarrier: sprint", "carrier"),
    ("models: [256gb-nightsky]\ncheckout: {mode: buy}", "checkout.mode"),
    ("models: [256gb-nightsky]\ncheckout: {mode: purchase}", "checkout.mode"),
])
def test_config_rejects(tmp_path, body, msg):
    with pytest.raises(ValueError, match=msg):
        load(write(tmp_path, body))


def test_example_config_loads():
    from pathlib import Path
    cfg = load(Path(__file__).parent.parent / "config.example.yaml")
    assert cfg.checkout_mode == "bag"


def test_courier_and_ship_quotes():
    from duo_tracker.availability import parse
    p = {"body": {"content": {"deliveryMessage": {PART: {"regular": {
        "idl": True, "stickyMessageIDL": "Courier delivery today, 4–6 pm",
        "stickyMessageSTH": "Delivers Jan 25 – Feb 1"}}}}}}
    av = parse(p, PART)
    assert av.courier_available and av.courier_quote.startswith("Courier")
    assert av.delivery_quote.startswith("Delivers Jan 25")
    p["body"]["content"]["deliveryMessage"][PART]["regular"]["stickyMessageIDL"] = "Courier delivery unavailable"
    assert not parse(p, PART).courier_available


def test_distance_and_pickup_quote_fallback():
    p = {"body": {"content": {"pickupMessage": {"stores": [
        {"storeName": "Near", "storeNumber": "R1", "storeDistanceWithUnit": "2.4 mi",
         "partsAvailability": {PART: {"pickupDisplay": "available",
                                      "messageTypes": {"regular": {"storePickupQuote": "Today at Near"}}}}},
        {"storeName": "Far", "storeNumber": "R2", "storeDistanceWithUnit": "48 km",
         "partsAvailability": {PART: {"pickupDisplay": "available"}}},
    ]}}}}
    av = parse(p, PART)
    assert av.stores[0].miles == 2.4 and av.stores[0].quote == "Today at Near"
    assert round(av.stores[1].miles, 1) == 29.8
    assert [s.store_name for s in matching_stores(av, [], max_miles=10)] == ["Near"]


def test_hit_keys_only_new_drops_alert():
    from duo_tracker.availability import hit_keys
    av = parse(payload([("Fifth Avenue", "New York", "available"), ("SoHo", "New York", "available")]), PART)
    k1 = hit_keys(PART, av, av.in_stock_stores, courier=True)
    assert set(k1) == {f"pickup:R000:{PART}", f"pickup:R001:{PART}"}
    # next round SoHo sells out, Fifth stays: nothing fresh
    av2 = parse(payload([("Fifth Avenue", "New York", "available"), ("SoHo", "New York", "unavailable")]), PART)
    k2 = hit_keys(PART, av2, av2.in_stock_stores, courier=True)
    assert [k for k in k2 if k not in k1] == []


def test_duplicate_models_collapse(tmp_path):
    cfg = load(write(tmp_path, "models: [256gb-nightsky, MK1F4LL/A]"))
    assert len(cfg.models) == 1 and cfg.interval_seconds == 30 and cfg.max_miles is None


def test_price_cap(tmp_path):
    cfg = load(write(tmp_path, "models: [256gb-nightsky, 2tb-starwhite]\ncheckout: {mode: submit}"))
    assert cfg.checkout_mode == "submit"
    assert cfg.cap_for(cfg.models[0]) == 2218.89 and cfg.cap_for(cfg.models[1]) == 3550.89
    cfg = load(write(tmp_path, "models: [256gb-nightsky]\ncheckout: {mode: submit, max_total: 2200}"))
    assert cfg.cap_for(cfg.models[0]) == 2200
