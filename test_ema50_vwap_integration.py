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
    assert result["cross_any"] == (result["cross_ema9"] or result["cross_ema50"])
    assert result["cross_trigger"] in ("EMA9_VWAP", "EMA50_VWAP", "NONE")
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
