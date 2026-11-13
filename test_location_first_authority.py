import pandas as pd

from roro_institutional import _zone_retest_candidates


def _context(**overrides):
    value = {
        "location_first_required": True,
        "reference_price": 100.0,
        "latest_timestamp": 1_700_000_000_000,
        "timeframe_ms": 900_000,
        "max_location_distance_atr": 1.5,
        "order_block": {
            "low": 99.0, "high": 100.5, "timestamp": 1_699_999_000_000,
            "impulse_timestamp": 1_699_999_100_000, "strength": 8.0,
            "distance_atr": 0.0,
        },
    }
    value.update(overrides)
    return value


def test_location_first_never_uses_naked_bos_level():
    zones = _zone_retest_candidates(_context(), fallback_level=101.0, atr=1.0, side="BUY")
    assert zones
    assert all(zone[2] != "BOS_LEVEL" for zone in zones)
    assert zones[0][2] == "ORDER_BLOCK"


def test_location_first_rejects_zone_far_from_current_price():
    context = _context(order_block={
        "low": 104.0, "high": 105.0, "timestamp": 1_699_999_000_000,
        "impulse_timestamp": 1_699_999_100_000, "strength": 8.0,
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_zone_without_confirmed_timestamp():
    context = _context(order_block={"low": 99.0, "high": 100.5, "strength": 8.0})
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_stale_zone():
    context = _context(order_block={
        "low": 99.0, "high": 100.5, "timestamp": 1_699_000_000_000,
        "impulse_timestamp": 1_699_000_000_000, "strength": 8.0,
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_legacy_callers_keep_bos_fallback():
    zones = _zone_retest_candidates({}, fallback_level=101.0, atr=1.0, side="BUY")
    assert zones == [(101.0, 101.0, "BOS_LEVEL", None)]
