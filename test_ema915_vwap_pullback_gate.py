import numpy as np
import pandas as pd

import roro_confluence as confluence


def _frame(side="BUY", count=80):
    start = 1795132800000
    rows = []
    for i in range(count):
        rows.append({
            "timestamp": start + i * 900_000,
            "open": 100.00,
            "high": 100.20,
            "low": 99.80,
            "close": 100.00,
            "volume": 1000.0,
        })
    frame = pd.DataFrame(rows)
    if side == "BUY":
        # Cross occurs four bars before the last closed candle.
        frame.loc[count - 2, ["open", "high", "low", "close"]] = [100.15, 100.30, 99.95, 100.10]
        frame.loc[count - 1, ["open", "high", "low", "close"]] = [100.15, 100.60, 100.05, 100.50]
    else:
        frame.loc[count - 2, ["open", "high", "low", "close"]] = [99.85, 100.20, 99.70, 100.05]
        frame.loc[count - 1, ["open", "high", "low", "close"]] = [99.85, 100.00, 99.40, 99.50]
    return frame


def _ema_factory(side="BUY"):
    def ema(values, period):
        if period == 9:
            out = np.full(len(values), 99.8 if side == "BUY" else 100.2, dtype=float)
            out[-4:] = 100.2 if side == "BUY" else 100.0
        elif period == 15:
            out = np.full(len(values), 100.0 if side == "BUY" else 100.1, dtype=float)
        else:
            raise AssertionError(f"unexpected EMA period: {period}")
        return pd.Series(out, index=values.index)
    return ema


def _run(monkeypatch, side="BUY", frame=None, vwap_value=None, current_price=None, now_offset=1_000):
    frame = frame if frame is not None else _frame(side)
    vwap_value = vwap_value if vwap_value is not None else (99.5 if side == "BUY" else 100.5)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(vwap_value, index=closed.index, dtype=float),
    )
    now_ms = int(frame.timestamp.iloc[-1] + 900_000 + now_offset)
    price = current_price if current_price is not None else (100.50 if side == "BUY" else 99.50)
    return confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, side, _ema_factory(side), timeframe_seconds=900, now_ms=now_ms,
        cross_lookback_candles=8, pullback_lookback_candles=5, current_price=price,
    )


def test_buy_requires_ema9_ema15_cross_vwap_pullback_rejection_and_break(monkeypatch):
    result = _run(monkeypatch, "BUY")
    assert result["ready"] is True
    assert result["cross_direction"] == "BULLISH"
    assert result["pullback_touch"] is True
    assert result["rejection_confirmed"] is True
    assert result["continuation_break"] is True
    assert "EMA9_EMA15" in result["signal_candle_id"]


def test_sell_is_directional_mirror_of_buy(monkeypatch):
    result = _run(monkeypatch, "SELL")
    assert result["ready"] is True
    assert result["cross_direction"] == "BEARISH"
    assert result["pullback_touch"] is True
    assert result["continuation_break"] is True


def test_missing_ema9_ema15_cross_fails_closed(monkeypatch):
    frame = _frame("BUY")
    def no_cross(values, period):
        value = 100.2 if period == 9 else 100.0
        return pd.Series(np.full(len(values), value), index=values.index)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", no_cross, now_ms=int(frame.timestamp.iloc[-1] + 901_000)
    )
    assert result["ready"] is False
    assert result["reason"] == "missing_fresh_ema9_ema15_cross"


def test_vwap_direction_must_agree_with_ema_cross(monkeypatch):
    result = _run(monkeypatch, "BUY", vwap_value=100.4)
    assert result["ready"] is False
    assert result["reason"] == "ema915_or_vwap_direction_not_aligned"


def test_pullback_is_mandatory_after_cross(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 2, ["high", "low", "close"]] = [100.60, 100.45, 100.50]
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.50, 100.80, 100.45, 100.70]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_ema_band_pullback"


def test_rejection_and_continuation_break_are_separate_requirements(monkeypatch):
    frame = _frame("BUY")
    # A bullish candle that fails to close above the previous candle high.
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.10, 100.30, 100.00, 100.25]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_continuation_break"


def test_continuation_without_rejection_wick_is_not_authorized(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.10, 100.60, 100.10, 100.50]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_directional_rejection"


def test_no_chase_guard_uses_actual_ticker_price(monkeypatch):
    result = _run(monkeypatch, "BUY", current_price=102.0)
    assert result["ready"] is False
    assert result["reason"] == "entry_window_missed_no_chase"


def test_stale_closed_candles_cannot_authorize_entry(monkeypatch):
    frame = _frame("BUY")
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index),
    )
    now_ms = int(frame.timestamp.iloc[-1] + 4 * 900_000)
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema_factory("BUY"), timeframe_seconds=900, now_ms=now_ms,
        current_price=100.5,
    )
    assert result["ready"] is False
    assert result["reason"] == "stale_closed_candle_data"


def test_all_execution_paths_use_primary_gate_and_live_last_mile_revalidates():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    execute_start = source.index("def execute_entry(")
    execute_end = source.find("\ndef ", execute_start + 1)
    execute = source[execute_start:execute_end]
    assert "_validate_ema915_vwap_pullback_entry_gate(" in execute
    assert "_validate_dual_ema_vwap_entry_gate(" not in execute
    open_position = source[source.index("def open_position("):]
    assert "[EMA915_FINAL_GATE]" in open_position
    assert "entry_gate_snapshot" in open_position
    assert "submit_market_entry(" in open_position
    scaling = source[source.index("def scaling_logic("):source.index("def council_exit(")]
    assert "_validate_ema915_vwap_pullback_entry_gate(" in scaling
    assert "EMA915_SCALE_IN_BLOCK" in scaling
    assert scaling.index('STATE["qty"] += actual_scale_qty') < scaling.index("journal.consume_event(event_id, scale_trade_id)")
