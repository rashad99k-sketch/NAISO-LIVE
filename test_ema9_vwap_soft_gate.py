import numpy as np
import pandas as pd

from roro_confluence import validate_dual_ema_vwap_entry_gate


def _frame(count=80):
    start = 1795132800000  # 2026-11-20 00:00 UTC
    rows = []
    for i in range(count):
        rows.append({
            "timestamp": start + i * 900_000,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
            "volume": 1000.0,
        })
    frame = pd.DataFrame(rows)
    now_ms = int(frame.timestamp.iloc[-1] + 900_000 + 1_000)
    return frame, now_ms


def _ema_with_last_cross(values, period):
    out = pd.Series(100.2 if period == 50 else 99.8, index=values.index, dtype=float)
    if period == 9:
        out.iloc[-1] = 100.2
    return out


def test_valid_ema9_cross_passes_without_any_ema50_cross():
    frame, now_ms = _frame()
    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", _ema_with_last_cross, timeframe_seconds=900, now_ms=now_ms,
    )
    assert result["ready"] is True
    assert result["ema9_cross"] == "BULLISH"
    assert result["ema50_cross"] is None
    assert result["reason"] == "fresh_closed_ema9_vwap_cross_ema50_soft_confirmation"
    assert result["signal_candle_id"].startswith("BUY:")
    assert result["cross_quality"] == "UNRATED_PENDING_CALIBRATION"


def test_strongly_opposed_ema50_is_diagnostic_not_a_hard_veto():
    frame, now_ms = _frame()

    def opposed_ema50(values, period):
        if period == 9:
            out = pd.Series(99.8, index=values.index, dtype=float)
            out.iloc[-1] = 100.2
            return out
        return pd.Series(np.linspace(101.0, 100.5, len(values)), index=values.index)

    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", opposed_ema50, timeframe_seconds=900, now_ms=now_ms,
    )
    assert result["ready"] is True
    assert result["ema50_alignment"] == "OPPOSED"
    assert result["ema50_distance_atr"] > 0
    assert result["quality_features"]["ema50_alignment"] == "OPPOSED"


def test_ema50_unavailable_does_not_block_valid_ema9_cross():
    frame, now_ms = _frame(count=35)

    def ema9_only(values, period):
        assert period == 9
        out = pd.Series(99.8, index=values.index, dtype=float)
        out.iloc[-1] = 100.2
        return out

    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", ema9_only, timeframe_seconds=900, now_ms=now_ms,
    )
    assert result["ready"] is True
    assert result["ema50"] is None
    assert result["ema50_alignment"] == "UNAVAILABLE"


def test_touch_without_a_strict_ema9_vwap_cross_is_not_authorized():
    frame, now_ms = _frame()

    def touching(values, period):
        out = pd.Series(99.8, index=values.index, dtype=float)
        out.iloc[-1] = 100.0
        return out

    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", touching, timeframe_seconds=900, now_ms=now_ms,
    )
    assert result["ready"] is False
    assert result["reason"] == "latest_closed_ema9_vwap_relationship_not_directional"


def test_stale_closed_cross_data_is_rejected():
    frame, now_ms = _frame()
    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", _ema_with_last_cross, timeframe_seconds=900,
        now_ms=now_ms + 4 * 900_000,
    )
    assert result["ready"] is False
    assert result["reason"] == "stale_closed_candle_data"


def test_missing_fresh_ema9_cross_is_rejected_even_if_ema50_is_above_vwap():
    frame, now_ms = _frame()

    def no_ema9_cross(values, period):
        return pd.Series(100.2, index=values.index, dtype=float)

    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", no_ema9_cross, timeframe_seconds=900, now_ms=now_ms,
    )
    assert result["ready"] is False
    assert result["reason"] == "missing_fresh_directional_ema9_vwap_cross"

def test_cross_candle_age_is_checked_separately_from_latest_data_age():
    frame, now_ms = _frame()

    def older_cross(values, period):
        out = pd.Series(99.8 if period == 9 else 100.2, index=values.index, dtype=float)
        if period == 9:
            out.iloc[-2:] = 100.2
        return out

    # The latest closed candle is only 20 minutes old (within the 30-minute
    # data freshness limit), but the cross candle itself is 35 minutes old.
    result = validate_dual_ema_vwap_entry_gate(
        frame, "BUY", older_cross, timeframe_seconds=900,
        now_ms=now_ms + 20 * 60_000,
    )
    assert result["ready"] is False
    assert result["reason"] == "stale_ema9_vwap_cross"
    assert result["data_age_seconds"] <= result["max_data_age_seconds"]
    assert result["cross_age_seconds"] > result["max_data_age_seconds"]
