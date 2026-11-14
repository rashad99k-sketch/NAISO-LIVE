"""Synthetic market and dashboard integration tests for Location-First entry.

All market data is deterministic and synthetic. No exchange credentials, live orders,
network calls, or real account state are used.
"""
import numpy as np
import pandas as pd
import pytest

from location_authority import validate_location_zone


NOW = 1_800_000_000_000
TF = 900_000


def _zone(kind="ORDER_BLOCK", side="BUY", low=99.0, high=100.0, age_bars=4):
    return {
        "kind": kind,
        "low": low,
        "high": high,
        "zone_definition": "BULLISH_LOW_TO_OPEN" if side == "BUY" else "BEARISH_OPEN_TO_HIGH",
        "direction": "BULLISH" if side == "BUY" else "BEARISH",
        "strength": 8.0,
        "status": "CONFIRMED" if kind in {"SUPPORT", "RESISTANCE"} else "UNFILLED",
        "size_atr": 0.2,
        "available_timestamp": NOW - age_bars * TF,
    }


@pytest.mark.parametrize("side,kind,zone", [
    ("BUY", "ORDER_BLOCK", _zone("ORDER_BLOCK", "BUY")),
    ("SELL", "ORDER_BLOCK", _zone("ORDER_BLOCK", "SELL", low=100.0, high=101.0)),
    ("BUY", "SUPPORT", _zone("SUPPORT", "BUY")),
    ("SELL", "RESISTANCE", _zone("RESISTANCE", "SELL", low=100.0, high=101.0)),
    ("BUY", "FVG", _zone("FVG", "BUY")),
    ("SELL", "FVG", _zone("FVG", "SELL", low=100.0, high=101.0)),
])
def test_synthetic_market_accepts_side_aligned_fresh_institutional_zone(side, kind, zone):
    price = 99.5 if side == "BUY" else 100.5
    result = validate_location_zone(zone, side, price, 1.0, NOW, TF)
    assert result["valid"] is True
    assert result["kind"] == kind
    assert result["age_bars"] == 4


def test_synthetic_market_accepts_exact_no_chase_boundary_1_5_atr():
    zone = _zone("ORDER_BLOCK", "BUY", low=99.0, high=100.0)
    result = validate_location_zone(zone, "BUY", 101.5, 1.0, NOW, TF, max_distance_atr=1.5)
    assert result["valid"] is True
    assert result["distance_atr"] == 1.5


def test_synthetic_market_rejects_price_just_beyond_no_chase_boundary():
    zone = _zone("ORDER_BLOCK", "BUY", low=99.0, high=100.0)
    result = validate_location_zone(zone, "BUY", 101.500001, 1.0, NOW, TF, max_distance_atr=1.5)
    assert result["valid"] is False
    assert result["reason"] == "zone_outside_no_chase_distance"


@pytest.mark.parametrize("side,kind,zone", [
    ("BUY", "ORDER_BLOCK", _zone("ORDER_BLOCK", "SELL", low=100.0, high=101.0)),
    ("SELL", "ORDER_BLOCK", _zone("ORDER_BLOCK", "BUY")),
    ("BUY", "RESISTANCE", _zone("RESISTANCE", "SELL", low=100.0, high=101.0)),
    ("SELL", "SUPPORT", _zone("SUPPORT", "BUY")),
    ("BUY", "FVG", _zone("FVG", "SELL", low=100.0, high=101.0)),
])
def test_synthetic_market_rejects_wrong_side_location(side, kind, zone):
    result = validate_location_zone(zone, side, 100.0, 1.0, NOW, TF)
    assert result["valid"] is False


def test_synthetic_market_accepts_zone_at_maximum_age_and_rejects_one_bar_older():
    accepted = validate_location_zone(_zone(age_bars=48), "BUY", 99.5, 1.0, NOW, TF)
    rejected = validate_location_zone(_zone(age_bars=49), "BUY", 99.5, 1.0, NOW, TF)
    assert accepted["valid"] is True
    assert rejected["valid"] is False
    assert rejected["reason"] == "stale_zone"


def test_synthetic_market_rejects_future_zone_timestamp():
    zone = _zone()
    zone["available_timestamp"] = NOW + TF
    result = validate_location_zone(zone, "BUY", 99.5, 1.0, NOW, TF)
    assert result["valid"] is False
    assert result["reason"] == "future_dated_zone"


def test_synthetic_market_rejects_invalid_or_mitigated_fvg():
    zone = _zone("FVG", "BUY")
    zone["status"] = "FULLY_FILLED"
    result = validate_location_zone(zone, "BUY", 99.5, 1.0, NOW, TF)
    assert result["valid"] is False
    assert result["reason"] == "fvg_not_actionable"


def test_support_resistance_zero_distance_is_not_replaced_with_missing_sentinel(monkeypatch):
    import ggp_institutional_bridge as bridge

    count = 80
    close = np.linspace(100.0, 101.0, count)
    frame = pd.DataFrame({
        "timestamp": NOW - (count - np.arange(count)) * TF,
        "open": close - 0.05,
        "high": close + 0.10,
        "low": close - 0.10,
        "close": close,
        "volume": np.full(count, 1000.0),
    })
    aligned = {"type": "SUPPORT", "low": 100.9, "high": 101.1, "status": "CONFIRMED",
               "strength": 7.0, "distance_atr": 0.0, "confirmed_at_timestamp": int(frame.timestamp.iloc[-3])}
    monkeypatch.setattr(bridge, "_atr", lambda _df: 1.0)
    monkeypatch.setattr(bridge, "classify_regime", lambda *_a, **_k: {"state": "RANGE"})
    monkeypatch.setattr(bridge, "detect_structure", lambda *_a, **_k: {})
    monkeypatch.setattr(bridge, "classify_liquidity", lambda *_a, **_k: type("L", (), {"event":"NONE","direction":"UNKNOWN","level":None})())
    monkeypatch.setattr(bridge, "find_causal_order_block", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "build_support_resistance_zones", lambda *_a, **_k: {
        "status": "VALID", "supports": [aligned], "resistances": []
    })
    monkeypatch.setattr(bridge, "detect_fvg", lambda *_a, **_k: None)
    result = bridge._build_context(frame, "BUY")
    assert result["support_resistance_aligned"] is True
    assert result["golden_zone"]["aligned_sr_zone"]["distance_atr"] == 0.0


def test_dashboard_health_and_data_routes_respond_without_exchange_calls(monkeypatch):
    import ggpbot

    monkeypatch.setattr(ggpbot, "get_balance_safe", lambda: 25.0)
    monkeypatch.setattr(ggpbot, "get_free_balance_safe", lambda: 20.0)
    monkeypatch.setattr(ggpbot, "get_dashboard_metrics", lambda: {
        "total_pnl": "0.00%", "total_pnl_usdt": 0.0, "last_trade": "N/A"
    })
    monkeypatch.setattr(ggpbot, "cleanup_watchlist", lambda: None)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", False)
    ggpbot.STATE["open"] = False
    ggpbot.DASHBOARD_STATE["position"] = None
    ggpbot.MEMORY["last_scan"] = 0
    ggpbot.MEMORY["regime"] = "SYNTHETIC_TEST"
    ggpbot.MEMORY["health"] = {"api": "TEST", "status": "HEALTHY"}
    ggpbot.MEMORY["watchlist"] = {}

    client = ggpbot.app.test_client()
    health = client.get("/health")
    data = client.get("/data")

    assert health.status_code == 200
    assert health.get_json()["ok"] is True
    assert data.status_code == 200
    payload = data.get_json()
    assert payload["balance"] == 25.0
    assert payload["free_balance"] == 20.0
    assert payload["mode"] == "PAPER"
    assert payload["regime"] == "SYNTHETIC_TEST"
    assert "watchlist" in payload
