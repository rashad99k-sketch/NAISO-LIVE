"""Early trend timing validation for closed-candle M15 institutional entries.

This module is analytical only. It does not create orders or alter sizing, SL/TP,
position management, or reversal authorization.
"""
from __future__ import annotations

import math
from typing import Any, Dict

import numpy as np
import pandas as pd

from roro_confluence import compute_session_vwap


def _wilder_rma(values: pd.Series, period: int) -> pd.Series:
    """SMA-seeded Wilder RMA; unlike a plain rolling mean it retains smoothing."""
    source = pd.to_numeric(values, errors="coerce").astype(float)
    out = pd.Series(np.nan, index=source.index, dtype=float)
    if period < 2 or len(source) < period:
        return out
    array = source.to_numpy(dtype=float)
    finite_starts = np.flatnonzero(np.isfinite(array))
    seed_start = None
    for candidate in finite_starts:
        candidate = int(candidate)
        if candidate + period <= len(array) and np.isfinite(array[candidate:candidate + period]).all():
            seed_start = candidate
            break
    if seed_start is None:
        return out
    seed_index = seed_start + period - 1
    out.iloc[seed_index] = float(np.mean(array[seed_start:seed_index + 1]))
    for i in range(seed_index + 1, len(array)):
        if not np.isfinite(array[i]):
            return out
        out.iloc[i] = (float(out.iloc[i - 1]) * (period - 1) + array[i]) / period
    return out


def _indicator_frame(frame: pd.DataFrame, period: int = 14) -> Dict[str, Any]:
    required = ("timestamp", "open", "high", "low", "close", "volume")
    if not isinstance(frame, pd.DataFrame) or len(frame) < max(55, period * 2 + 2):
        return {"valid": False, "reason": "insufficient_closed_candles"}
    if any(column not in frame.columns for column in required):
        return {"valid": False, "reason": "required_ohlcv_columns_missing"}
    x = frame.loc[:, required].copy()
    for column in required:
        x[column] = pd.to_numeric(x[column], errors="coerce")
    if x.isna().any().any() or not x.timestamp.is_monotonic_increasing or x.timestamp.duplicated().any():
        return {"valid": False, "reason": "invalid_or_non_monotonic_closed_candles"}
    if (x.volume < 0).any() or (x[["open", "high", "low", "close"]] <= 0).any().any():
        return {"valid": False, "reason": "invalid_ohlcv_values"}
    if (x.high < x[["open", "close", "low"]].max(axis=1)).any():
        return {"valid": False, "reason": "invalid_ohlc_high"}
    if (x.low > x[["open", "close", "high"]].min(axis=1)).any():
        return {"valid": False, "reason": "invalid_ohlc_low"}

    previous_close = x.close.shift(1)
    true_range = pd.concat(
        [x.high - x.low, (x.high - previous_close).abs(), (x.low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = _wilder_rma(true_range, period)

    up_move = x.high.diff()
    down_move = -x.low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=x.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=x.index
    )
    plus_smoothed = _wilder_rma(plus_dm, period)
    minus_smoothed = _wilder_rma(minus_dm, period)
    plus_di = 100.0 * plus_smoothed / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_smoothed / atr.replace(0, np.nan)
    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    adx = _wilder_rma(dx, period)

    ema9 = x.close.ewm(span=9, adjust=False, min_periods=9).mean()
    ema20 = x.close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = x.close.ewm(span=50, adjust=False, min_periods=50).mean()

    try:
        timestamp_deltas = x.timestamp.diff().dropna()
        timeframe_ms = int(timestamp_deltas.tail(20).median())
        if timeframe_ms <= 0 or not np.isfinite(timeframe_ms):
            raise ValueError("invalid_timeframe")
        session_vwap = compute_session_vwap(x, timeframe_seconds=max(1, timeframe_ms // 1000))
    except Exception:
        session_vwap = None
    if session_vwap is None or len(session_vwap) != len(x):
        return {"valid": False, "reason": "complete_utc_session_vwap_unavailable"}

    current_session = int(x.timestamp.iloc[-1] // 86_400_000)
    session_mask = (x.timestamp.astype("int64") // 86_400_000) == current_session
    if not bool(session_mask.any()) or not np.isfinite(session_vwap.loc[session_mask].to_numpy(dtype=float)).all():
        return {"valid": False, "reason": "session_vwap_values_unavailable"}

    return {
        "valid": True, "frame": x, "atr": atr, "plus_di": plus_di, "minus_di": minus_di,
        "adx": adx, "ema9": ema9, "ema20": ema20, "ema50": ema50, "vwap": session_vwap,
    }


def evaluate_early_trend_timing(frame: pd.DataFrame, side: str,
                               max_ema9_extension_atr: float = 1.35,
                               max_vwap_extension_atr: float = 2.5) -> Dict[str, Any]:
    """Classify a continuation trigger as early/healthy, waiting, or too extended.

    Inputs must already be closed candles. ADX is used as directional-strength
    confirmation, never as a standalone trend/exhaustion signal. EMA9/20 provide
    responsive timing; EMA50 is a slower context filter; VWAP is UTC-session OHLC4.
    """
    direction = str(side or "").strip().upper()
    if direction not in {"BUY", "SELL"}:
        return {"valid": False, "status": "DATA_UNAVAILABLE", "reason": "invalid_side"}

    values = _indicator_frame(frame)
    if not values.get("valid"):
        return {"valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": str(values.get("reason", "indicator_data_unavailable"))}

    x = values["frame"]
    atr_s, adx_s = values["atr"], values["adx"]
    e9, e20, e50, vwap = values["ema9"], values["ema20"], values["ema50"], values["vwap"]
    required_indices = (-1, -2, -4, -7)
    series = (atr_s, adx_s, e9, e20, e50, vwap)
    if any(len(s) < 7 or any(not np.isfinite(float(s.iloc[i])) for i in required_indices) for s in series):
        return {"valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "indicator_warmup_or_nan"}

    atr = float(atr_s.iloc[-1])
    close = float(x.close.iloc[-1])
    open_price = float(x.open.iloc[-1])
    candle_range = float(x.high.iloc[-1] - x.low.iloc[-1])
    ema9_now, ema20_now, ema50_now = float(e9.iloc[-1]), float(e20.iloc[-1]), float(e50.iloc[-1])
    ema9_slope = float(e9.iloc[-1] - e9.iloc[-4])
    ema20_slope = float(e20.iloc[-1] - e20.iloc[-4])
    ema50_slope = float(e50.iloc[-1] - e50.iloc[-4])
    vwap_now = float(vwap.iloc[-1])
    vwap_slope = float(vwap.iloc[-1] - vwap.iloc[-4])
    adx_now = float(adx_s.iloc[-1])
    adx_slope = float(adx_s.iloc[-1] - adx_s.iloc[-4])
    plus_di = float(values["plus_di"].iloc[-1])
    minus_di = float(values["minus_di"].iloc[-1])
    if not all(math.isfinite(v) for v in (
        atr, close, open_price, candle_range, ema9_now, ema20_now, ema50_now,
        ema9_slope, ema20_slope, ema50_slope, vwap_now, vwap_slope,
        adx_now, adx_slope, plus_di, minus_di,
    )) or atr <= 0 or vwap_now <= 0:
        return {"valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "non_finite_indicator_snapshot"}

    buy = direction == "BUY"
    directional = 1.0 if buy else -1.0
    ema_gap = float(e9.iloc[-1] - e20.iloc[-1])
    # Cross events are measured on completed candles only. Either EMA9/VWAP
    # (early trigger) or EMA50/VWAP (slower confirmation) may start evaluation.
    # A cross starts evaluation; it is never an order authorization by itself.
    fresh_ema_cross_recent = False
    fresh_ema9_vwap_cross = False
    fresh_ema50_vwap_cross = False
    for idx in range(max(1, len(x) - 6), len(x)):
        prior_gap = float(e9.iloc[idx - 1] - vwap.iloc[idx - 1])
        current_gap = float(e9.iloc[idx] - vwap.iloc[idx])
        if (prior_gap <= 0 < current_gap) if buy else (prior_gap >= 0 > current_gap):
            fresh_ema9_vwap_cross = True
        prior_slow_gap = float(e50.iloc[idx - 1] - vwap.iloc[idx - 1])
        current_slow_gap = float(e50.iloc[idx] - vwap.iloc[idx])
        if (prior_slow_gap <= 0 < current_slow_gap) if buy else (prior_slow_gap >= 0 > current_slow_gap):
            fresh_ema50_vwap_cross = True
        prior_ema_gap = float(e9.iloc[idx - 1] - e20.iloc[idx - 1])
        current_ema_gap = float(e9.iloc[idx] - e20.iloc[idx])
        if (prior_ema_gap <= 0 < current_ema_gap) if buy else (prior_ema_gap >= 0 > current_ema_gap):
            fresh_ema_cross_recent = True
    fresh_vwap_cross = fresh_ema9_vwap_cross or fresh_ema50_vwap_cross

    adx_cross_recent = False
    for idx in range(max(1, len(x) - 6), len(x)):
        previous_adx, current_adx = float(adx_s.iloc[idx - 1]), float(adx_s.iloc[idx])
        if math.isfinite(previous_adx) and math.isfinite(current_adx) and previous_adx < 18.0 <= current_adx:
            adx_cross_recent = True
            break

    ema_aligned = ema_gap * directional > 0 and ema9_slope * directional > 0
    ema20_not_opposing = ema20_slope * directional >= -0.05 * atr
    ema50_not_strongly_opposing = ema50_slope * directional >= -0.12 * atr
    price_on_correct_side = close > vwap_now if buy else close < vwap_now
    price_holds_ema9 = close > ema9_now if buy else close < ema9_now
    vwap_not_opposing = vwap_slope * directional >= -0.05 * atr
    di_spread = (plus_di - minus_di) * directional
    adx_confirmed = adx_now >= 18.0 and (adx_slope > 0.0 or (adx_now >= 25.0 and adx_slope >= -0.5))
    ema9_extension = abs(close - ema9_now) / atr
    vwap_extension = abs(close - vwap_now) / atr
    impulse_extension = candle_range / atr

    diagnostics = {
        "adx": round(adx_now, 3), "adx_slope_3": round(adx_slope, 3),
        "di_spread": round(di_spread, 3), "ema9": ema9_now, "ema20": ema20_now,
        "ema50": ema50_now, "ema9_slope_3": round(ema9_slope, 10),
        "ema20_slope_3": round(ema20_slope, 10), "ema50_slope_3": round(ema50_slope, 10),
        "vwap": vwap_now, "vwap_slope_3": round(vwap_slope, 10),
        "atr14_wilder": atr, "ema9_extension_atr": round(ema9_extension, 4),
        "vwap_extension_atr": round(vwap_extension, 4),
        "trigger_candle_range_atr": round(impulse_extension, 4),
        "fresh_ema9_ema20_cross": bool(fresh_ema_cross_recent),
        "fresh_ema9_vwap_cross": bool(fresh_ema9_vwap_cross),
        "fresh_ema50_vwap_cross": bool(fresh_ema50_vwap_cross),
        "cross_trigger": ("EMA9_VWAP" if fresh_ema9_vwap_cross else "EMA50_VWAP" if fresh_ema50_vwap_cross else "EMA9_EMA20" if fresh_ema_cross_recent else "NONE"),
        "fresh_adx_18_cross": bool(adx_cross_recent),
        "candle_timestamp": int(x.timestamp.iloc[-1]),
    }

    if not ema_aligned:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "ema9_ema20_direction_or_slope_not_confirmed"}
    if not ema20_not_opposing or not ema50_not_strongly_opposing:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "higher_ema_slope_strongly_opposes_entry"}
    if not price_on_correct_side or not vwap_not_opposing:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "session_vwap_not_reclaimed_or_slope_opposes_entry"}
    if not price_holds_ema9:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "closed_price_not_holding_ema9"}
    if di_spread < 2.0:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "directional_di_spread_not_confirmed"}
    # High ADX is not exhaustion by itself. Only a high, falling ADX combined
    # with price extension is treated as a late-stage continuation warning.
    if adx_now >= 45.0 and adx_slope < -1.0 and (ema9_extension > 0.8 or vwap_extension > 1.4):
        return {**diagnostics, "valid": False, "status": "NO_CHASE",
                "reason": "high_adx_fading_with_price_extension"}
    # Preserve healthy mature trends: a stable ADX >= 25 remains acceptable;
    # new/early trends must show ADX >= 18 and a positive slope.
    if not adx_confirmed:
        return {**diagnostics, "valid": False, "status": "WAITING_FOR_CONFIRMATION",
                "reason": "adx_below_18_or_weakening"}
    if ema9_extension > float(max_ema9_extension_atr) or vwap_extension > float(max_vwap_extension_atr):
        return {**diagnostics, "valid": False, "status": "NO_CHASE",
                "reason": "price_overextended_from_ema9_or_session_vwap"}
    if impulse_extension > 1.8:
        return {**diagnostics, "valid": False, "status": "NO_CHASE",
                "reason": "trigger_candle_already_overextended"}

    phase = "EARLY_EXPANSION" if fresh_vwap_cross or fresh_ema_cross_recent or adx_cross_recent else "HEALTHY_CONTINUATION"
    return {**diagnostics, "valid": True, "status": "CONFIRMED",
            "phase": phase, "reason": "ema9_or_ema50_vwap_cross_with_adx_di_confirmation" if fresh_vwap_cross else "ema_vwap_adx_di_early_timing_confirmed"}
