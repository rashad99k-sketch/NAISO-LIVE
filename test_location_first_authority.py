from roro_institutional import _location_event_id, _zone_retest_candidates
from location_authority import validate_location_zone


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
            "distance_atr": 0.0, "zone_definition": "BULLISH_LOW_TO_OPEN",
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
        "zone_definition": "BULLISH_LOW_TO_OPEN",
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_zone_without_confirmed_timestamp():
    context = _context(order_block={"low": 99.0, "high": 100.5, "strength": 8.0, "zone_definition": "BULLISH_LOW_TO_OPEN"})
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_stale_zone():
    context = _context(order_block={
        "low": 99.0, "high": 100.5, "timestamp": 1_699_000_000_000,
        "impulse_timestamp": 1_699_000_000_000, "strength": 8.0,
        "zone_definition": "BULLISH_LOW_TO_OPEN",
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_legacy_callers_keep_bos_fallback():
    zones = _zone_retest_candidates({}, fallback_level=101.0, atr=1.0, side="BUY")
    assert zones == [(101.0, 101.0, "BOS_LEVEL", None)]



def test_same_zone_same_structure_is_deduplicated_but_new_structure_is_allowed():
    first = _location_event_id("BTC/USDT", "BUY", "ORDER_BLOCK", 1000, 2000)
    repeated = _location_event_id("BTC/USDT", "BUY", "ORDER_BLOCK", 1000, 2000)
    new_structure = _location_event_id("BTC/USDT", "BUY", "ORDER_BLOCK", 1000, 3000)
    new_zone = _location_event_id("BTC/USDT", "BUY", "ORDER_BLOCK", 1500, 2000)
    assert first == repeated
    assert first != new_structure
    assert first != new_zone


def test_location_first_rejects_weak_order_block():
    context = _context(order_block={
        "low": 99.0, "high": 100.5, "timestamp": 1_699_999_000_000,
        "impulse_timestamp": 1_699_999_100_000, "strength": 4.9, "zone_definition": "BULLISH_LOW_TO_OPEN",
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []



def test_location_first_rejects_opposite_direction_order_block():
    context = _context(order_block={
        "low": 99.0, "high": 100.5, "timestamp": 1_699_999_000_000,
        "impulse_timestamp": 1_699_999_100_000, "strength": 8.0,
        "zone_definition": "BEARISH_OPEN_TO_HIGH",
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_opposite_direction_fvg():
    context = _context(order_block=None, fvg={
        "lower": 99.0, "upper": 100.5, "timestamp": 1_699_999_100_000,
        "direction": "BEARISH", "status": "UNFILLED", "size_atr": 0.2,
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_rejects_future_dated_zone():
    context = _context(order_block={
        "low": 99.0, "high": 100.5, "timestamp": 1_700_000_100_000,
        "impulse_timestamp": 1_700_000_100_000, "strength": 8.0,
        "zone_definition": "BULLISH_LOW_TO_OPEN",
    })
    assert _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY") == []


def test_location_first_accepts_side_aligned_unfilled_fvg():
    context = _context(order_block=None, fvg={
        "lower": 99.0, "upper": 100.5, "timestamp": 1_699_999_100_000,
        "direction": "BULLISH", "status": "UNFILLED", "size_atr": 0.2,
    })
    zones = _zone_retest_candidates(context, fallback_level=101.0, atr=1.0, side="BUY")
    assert zones == [(99.0, 100.5, "FVG", 1_699_999_100_000)]



def test_central_location_authority_accepts_fresh_side_aligned_ob():
    result = validate_location_zone({
        "kind": "ORDER_BLOCK", "low": 99.0, "high": 100.5,
        "zone_definition": "BULLISH_LOW_TO_OPEN", "strength": 8.0,
        "available_timestamp": 1_699_999_100_000,
    }, "BUY", 100.0, 1.0, 1_700_000_000_000, 900_000)
    assert result["valid"] is True
    assert result["distance_atr"] == 0.0


def test_central_location_authority_rejects_wrong_side_fvg():
    result = validate_location_zone({
        "kind": "FVG", "low": 99.0, "high": 100.5,
        "direction": "BEARISH", "status": "UNFILLED", "size_atr": 0.2,
        "available_timestamp": 1_699_999_100_000,
    }, "BUY", 100.0, 1.0, 1_700_000_000_000, 900_000)
    assert result["valid"] is False
    assert result["reason"] == "fvg_direction_mismatch"


def test_central_location_authority_rejects_future_timestamp():
    result = validate_location_zone({
        "kind": "ORDER_BLOCK", "low": 99.0, "high": 100.5,
        "zone_definition": "BULLISH_LOW_TO_OPEN", "strength": 8.0,
        "available_timestamp": 1_700_000_100_000,
    }, "BUY", 100.0, 1.0, 1_700_000_000_000, 900_000)
    assert result["valid"] is False
    assert result["reason"] == "future_dated_zone"


def test_central_location_authority_accepts_confirmed_aligned_support():
    result = validate_location_zone({
        "kind": "SUPPORT", "low": 99.0, "high": 100.5,
        "status": "CONFIRMED", "strength": 6.0,
        "available_timestamp": 1_699_999_100_000,
    }, "BUY", 100.0, 1.0, 1_700_000_000_000, 900_000)
    assert result["valid"] is True
