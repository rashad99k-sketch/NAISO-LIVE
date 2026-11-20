import numpy as np
import pandas as pd
import pytest

from roro_trend_timing import _wilder_rma, evaluate_early_trend_timing


def make_starting_trend(side="BUY", start_index=116, start_timestamp=0, late_impulse=False, count=120):
    volume_start = max(0, count - 24)
    closes = []
    for i in range(count):
        if i < 60:
            close = 100.0 - 0.05 * i + 0.04 * np.sin(i * 0.8)
        elif i < start_index:
            close = 100.0 - 0.05 * 60 + 0.05 * np.sin(i * 0.7)
        else:
            close = 100.0 - 0.05 * 60 + 0.15 * (i - start_index + 1) + 0.02 * np.sin(i * 0.8)
        closes.append(close)

    rows = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i else close
        high = max(open_price, close) + 0.12
        low = min(open_price, close) - 0.12
        volume = 100.0 if i < volume_start else 250.0 + (i - volume_start) * 30.0
        rows.append({
            "timestamp": int(start_timestamp + i * 900_000),
            "open": open_price, "high": high, "low": low,
            "close": close, "volume": volume,
        })

    frame = pd.DataFrame(rows)
    if side == "SELL":
        mirrored_open = 200.0 - pd.DataFrame(rows)["open"]
        mirrored_high = 200.0 - pd.DataFrame(rows)["low"]
        mirrored_low = 200.0 - pd.DataFrame(rows)["high"]
        mirrored_close = 200.0 - pd.DataFrame(rows)["close"]
        frame["open"] = mirrored_open
        frame["high"] = mirrored_high
        frame["low"] = mirrored_low
        frame["close"] = mirrored_close

    if late_impulse:
        idx = frame.index[-1]
        previous = float(frame.close.iloc[-2])
        frame.loc[idx, "open"] = previous
        frame.loc[idx, "close"] = previous + (2.0 if side == "BUY" else -2.0)
        frame.loc[idx, "high"] = max(frame.loc[idx, "open"], frame.loc[idx, "close"]) + 0.1
        frame.loc[idx, "low"] = min(frame.loc[idx, "open"], frame.loc[idx, "close"]) - 0.1
        frame.loc[idx, "volume"] = float(frame.volume.iloc[-2]) * 1.5
    return frame


def test_wilder_rma_uses_sma_seed_then_recursive_smoothing():
    result = _wilder_rma(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert result.iloc[0:2].isna().all()
    assert result.iloc[2] == 2.0
    assert result.iloc[3] == pytest.approx(8.0 / 3.0)
    assert result.iloc[4] == pytest.approx(31.0 / 9.0)


def test_fresh_m15_trend_start_is_confirmed_without_waiting_for_ema50_cross():
    result = evaluate_early_trend_timing(make_starting_trend("BUY"), "BUY")
    assert result["valid"] is True
    assert result["status"] == "CONFIRMED"
    assert result["phase"] == "EARLY_EXPANSION"
    assert result["ema20"] < result["ema50"]  # EMA20/EMA50 has not crossed yet.
    assert result["adx"] >= 18.0
    assert result["adx_slope_3"] > 0
    assert result["di_spread"] >= 2.0
    assert result["vwap_extension_atr"] <= 2.5


def test_sell_timing_is_the_directional_mirror_of_buy():
    result = evaluate_early_trend_timing(make_starting_trend("SELL"), "SELL")
    assert result["valid"] is True
    assert result["status"] == "CONFIRMED"


def test_extended_impulse_is_no_chase_not_a_late_market_entry():
    result = evaluate_early_trend_timing(
        make_starting_trend("BUY", late_impulse=True), "BUY"
    )
    assert result["valid"] is False
    assert result["status"] == "NO_CHASE"
    assert result["reason"] in {
        "price_overextended_from_ema9_or_session_vwap",
        "trigger_candle_already_overextended",
        "high_adx_fading_with_price_extension",
    }


def test_missing_start_of_utc_session_fails_closed_for_vwap():
    # 06:00 UTC start means the frame omits the session's first M15 candle.
    start = 6 * 60 * 60 * 1000
    result = evaluate_early_trend_timing(
        make_starting_trend("BUY", start_index=49, start_timestamp=start, count=60), "BUY"
    )
    assert result["valid"] is False
    assert result["status"] == "WAITING_FOR_CONFIRMATION"
    assert result["reason"] == "complete_utc_session_vwap_unavailable"



def test_continuation_engine_uses_early_alignment_and_expires_old_retests():
    from pathlib import Path

    source = Path("roro_institutional.py").read_text(encoding="utf-8")
    start = source.index("class ContinuationEngine:")
    end = source.index("class UnifiedEntryAuthority:", start)
    continuation = source[start:end]
    assert "legacy_aligned or early_timing.get(\"valid\")" in continuation
    assert "if r - bos_idx > 8:" in continuation
    assert "(len(x) - 1 - retest_idx) > 3" in continuation
    assert "evaluate_early_trend_timing(x, side)" in continuation



def test_vwap_cross_diagnostics_are_exposed_for_both_fast_and_slow_emas():
    result = evaluate_early_trend_timing(make_starting_trend("BUY"), "BUY")
    assert "fresh_ema9_vwap_cross" in result
    assert "fresh_ema50_vwap_cross" in result
    assert result["cross_trigger"] in {"EMA9_VWAP", "EMA50_VWAP", "EMA9_EMA20", "NONE"}
    assert isinstance(result["fresh_ema9_vwap_cross"], bool)
    assert isinstance(result["fresh_ema50_vwap_cross"], bool)


def test_invalid_data_does_not_authorize_vwap_cross_entry():
    frame = make_starting_trend("BUY").copy()
    frame.loc[frame.index[-1], "volume"] = -1.0
    result = evaluate_early_trend_timing(frame, "BUY")
    assert result["valid"] is False
    assert result["status"] == "WAITING_FOR_CONFIRMATION"

def test_invalid_side_fails_closed():
    result = evaluate_early_trend_timing(make_starting_trend(), "HOLD")
    assert result["valid"] is False
    assert result["reason"] == "invalid_side"
