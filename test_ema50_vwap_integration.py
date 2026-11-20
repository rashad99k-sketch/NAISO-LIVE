import numpy as np
import pandas as pd

from roro_confluence import ema9_vwap_proximity


def test_ema50_vwap_is_added_without_changing_legacy_ema9_cross_contract():
    start = 1795132800000  # 2026-11-20 00:00 UTC
    rows = []
    for i in range(160):
        close = 100.0 + 0.025 * i + 0.22 * np.sin(i * 0.31)
        open_price = close - (0.08 if i % 2 == 0 else -0.08)
        rows.append({
            "timestamp": start + i * 900_000,
            "open": open_price,
            "high": max(open_price, close) + 0.15,
            "low": min(open_price, close) - 0.15,
            "close": close,
            "volume": 1000.0 + (i % 7) * 75.0,
        })
    frame = pd.DataFrame(rows)
    now_ms = int(frame.timestamp.iloc[-1] + 900_000 + 1_000)

    result = ema9_vwap_proximity(
        frame,
        lambda values, period: values.ewm(span=period, adjust=False, min_periods=period).mean(),
        ema_len=9,
        timeframe_seconds=900,
        now_ms=now_ms,
    )

    assert result is not None
    assert result["cross"] == result["cross_ema9"]
    assert result["cross_ema50"] in (None, "BULLISH", "BEARISH")
    expected_conflict = bool(result["cross_ema9"] and result["cross_ema50"]
                             and result["cross_ema9"] != result["cross_ema50"])
    assert result["cross_conflict"] is expected_conflict
    assert result["cross_any"] == (None if expected_conflict else (result["cross_ema9"] or result["cross_ema50"]))
    assert result["cross_trigger"] in ("EMA9_VWAP", "EMA50_VWAP", "NONE", "CONFLICT")
    assert result["ema50"] is not None
    assert result["ema50_direction"] in ("BUY", "SELL", "NEUTRAL")
    assert result["candle_timestamp"] == int(frame.timestamp.iloc[-1])


def test_ema50_warmup_does_not_break_legacy_ema9_proximity():
    start = 1795132800000
    rows = []
    for i in range(35):
        close = 50.0 + i * 0.01
        rows.append({
            "timestamp": start + i * 900_000,
            "open": close - 0.02,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 500.0,
        })
    frame = pd.DataFrame(rows)
    result = ema9_vwap_proximity(
        frame,
        lambda values, period: values.ewm(span=period, adjust=False, min_periods=period).mean(),
        ema_len=9,
        timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000),
    )
    assert result is not None
    assert result["ema9"] > 0
    assert result["ema50"] is None
    assert result["cross_ema50"] is None
    assert result["cross"] == result["cross_ema9"]


def test_live_profit_manager_consumes_cross_diagnostics_without_gating_position_protection():
    from pathlib import Path

    source = Path("ggpbot.py").read_text(encoding="utf-8")
    assert '_roro_ema9_vwap_proximity(' in source
    assert 'STATE["fresh_ema9_vwap_cross"]' in source
    assert 'STATE["fresh_ema50_vwap_cross"]' in source
    assert '"vwap_cross_trigger": str(STATE.get("vwap_cross_trigger")' in source
    # A new cross is diagnostic/classification context; the open-position
    # manager must not require a cross to remain active.
    manager_start = source.index("    def manage_live_trade(self):")
    apply_start = source.index("    def _apply_management(self, symbol, now):", manager_start)
    manage_body = source[manager_start:apply_start]
    assert 'if not (STATE.get("open") and STATE.get("current_symbol"))' in manage_body
    assert "if not STATE.get(\"fresh_ema9_vwap_cross\")" not in manage_body
    assert "if not STATE.get(\"fresh_ema50_vwap_cross\")" not in manage_body


def test_watchlist_candidate_priority_uses_directional_cross_but_keeps_structural_authority():
    from pathlib import Path

    source = Path("ggp_institutional_bridge.py").read_text(encoding="utf-8")
    assert 'prior.get("ema9_vwap_proximity")' in source
    assert 'cross_direction == expected_cross' in source
    assert "vwap_cross_priority" in source
    assert "AUTHORITY.decide" in source
    assert "execute_entry remain the sole entry authorization path" in source


def test_cross_timestamp_fields_are_provided_to_profit_and_dashboard_context():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    assert '"cross_age_seconds": _cross_age if _cross_event else None' in source
    assert '"cross_max_age_seconds": _cross_max_age' in source
    assert '"vwap_cross_age_seconds": STATE.get("vwap_cross_age_seconds")' in source
    assert 'STATE["fresh_ema9_vwap_cross"] = bool(_cross_snapshot.get("cross_ema9") and _cross_snapshot.get("cross_fresh"))' in source
    assert 'STATE["fresh_ema50_vwap_cross"] = bool(_cross_snapshot.get("cross_ema50") and _cross_snapshot.get("cross_fresh"))' in source


def test_scanner_persists_cross_event_age_and_observation_timestamp():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    assert '"observation_age_seconds": 0.0' in source
    assert '"cross_age_seconds": _proximity_cross_age' in source
    assert '"cross_fresh": bool(_proximity_cross_event and _proximity_cross_age <= _proximity_cross_max_age)' in source
