"""Regression tests for the Sniper V2 integration boundary (no exchange calls)."""
import math
import pandas as pd
import pytest
import ggpbot


def _frame(n=20):
    ts = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    close = [100.0 + math.sin(i / 3.0) for i in range(n)]
    return pd.DataFrame({
        "timestamp": ts,
        "open": [x - 0.1 for x in close],
        "high": [x + 0.5 for x in close],
        "low": [x - 0.5 for x in close],
        "close": close,
        "volume": [1000.0] * n,
    })


def test_sniper_v2_tp_levels_are_fixed_move_percentages():
    assert ggpbot.SNIPER_V2_TP1_MOVE_PCT == pytest.approx(0.40)
    assert ggpbot.SNIPER_V2_TP2_MOVE_PCT == pytest.approx(1.00)


def test_pivot_high_waits_for_three_right_candles():
    frame = _frame(20)
    frame.loc[16, "high"] = 110.0
    frame.loc[17, "high"] = 105.0
    frame.loc[18, "high"] = 104.0
    frame.loc[19, "high"] = 103.0
    assert ggpbot.is_pivot_high(frame, lookback=3)
    frame.loc[19, "high"] = 111.0
    assert not ggpbot.is_pivot_high(frame, lookback=3)


def test_pivot_low_waits_for_three_right_candles():
    frame = _frame(20)
    frame.loc[16, "low"] = 90.0
    frame.loc[17, "low"] = 95.0
    frame.loc[18, "low"] = 96.0
    frame.loc[19, "low"] = 97.0
    assert ggpbot.is_pivot_low(frame, lookback=3)
    frame.loc[19, "low"] = 89.0
    assert not ggpbot.is_pivot_low(frame, lookback=3)


def test_sniper_v2_pauses_when_position_open(monkeypatch):
    monkeypatch.setattr(ggpbot, "SNIPER_V2_ENABLED", True)
    monkeypatch.setitem(ggpbot.STATE, "open", True)
    assert ggpbot.sniper_engine_v2() is False
    monkeypatch.setitem(ggpbot.STATE, "open", False)
