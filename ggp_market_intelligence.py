"""Market structure lifecycle intelligence for GGP.

Pure, deterministic, execution-free classifiers.  This module describes the
state of a reference price zone; it never places, modifies, or cancels orders.
Only completed OHLCV candles can confirm a structural event.  Missing or stale
inputs fail closed to UNKNOWN/INSUFFICIENT_DATA.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from math import isfinite
from typing import Any, Dict, Optional, Sequence, Tuple


class TradingMode(str, Enum):
    SCALP = "SCALP"
    TREND = "TREND"
    SNIPER = "SNIPER"
    UNCLASSIFIED = "UNCLASSIFIED"


class EntryBehavior(str, Enum):
    PULLBACK = "PULLBACK"
    MICRO_PULLBACK = "MICRO_PULLBACK"
    RETEST = "RETEST"
    WAIT_FOR_STRUCTURE = "WAIT_FOR_STRUCTURE"


class ZoneEvent(str, Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INSIDE_ZONE = "INSIDE_ZONE"
    APPROACHING = "APPROACHING"
    WICK_SWEEP = "WICK_SWEEP"
    BREAKOUT_PENDING = "BREAKOUT_PENDING"
    BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    ACCEPTED_ABOVE = "ACCEPTED_ABOVE"
    ACCEPTED_BELOW = "ACCEPTED_BELOW"
    STRUCTURE_WEAKENING = "STRUCTURE_WEAKENING"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"


@dataclass(frozen=True)
class IntelligenceDecision:
    mode: str
    entry_behavior: str
    zone_event: str
    confidence: float
    reference_level: Optional[float]
    reference_low: Optional[float]
    reference_high: Optional[float]
    reason_codes: Tuple[str, ...]
    confirmed: bool
    source_timestamp: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def classify_trading_mode(*, regime: str = "UNKNOWN", adx: Any = None,
                          continuation_probability: Any = None,
                          liquidity_quality: str = "UNKNOWN",
                          displacement_atr: Any = None,
                          sweep_confirmed: bool = False,
                          mss_confirmed: bool = False,
                          causal_zone_valid: bool = False,
                          data_quality: str = "UNKNOWN") -> Tuple[str, float, Tuple[str, ...]]:
    """Conservative mode label; no mode grants entry permission by itself."""
    reasons = []
    if str(data_quality).upper() not in {"VALID", "VALID_CLOSED_CANDLES", "FRESH"}:
        return TradingMode.UNCLASSIFIED.value, 0.0, ("data_quality_not_confirmed",)
    regime = str(regime or "UNKNOWN").upper()
    adx_v, cont, disp = _num(adx), _num(continuation_probability), _num(displacement_atr)

    # Sniper is a structural setup class, not a high score shortcut.
    if sweep_confirmed and mss_confirmed and causal_zone_valid:
        return TradingMode.SNIPER.value, 0.90, ("liquidity_sweep", "mss_confirmed", "causal_zone_valid")

    if regime in {"STRONG_TREND", "VERY_STRONG", "STRONG_TRENDING", "EXPANSION", "TREND"} and (
        adx_v is not None and adx_v >= 22 and cont is not None and cont >= 0.60
    ):
        return TradingMode.TREND.value, 0.82, ("directional_regime", "adx_confirmed", "continuation_confirmed")

    if regime in {"RANGE", "CHOPPY", "COMPRESSION", "RANGE_CHOP"} and (
        adx_v is not None and adx_v < 22 and cont is not None and cont < 0.60
        and str(liquidity_quality).upper() in {"GOOD", "ADEQUATE", "HIGH"}
    ):
        return TradingMode.SCALP.value, 0.72, ("range_regime", "weak_trend_strength", "liquidity_adequate")

    if disp is not None and disp >= 1.2 and cont is not None and cont >= 0.65:
        return TradingMode.TREND.value, 0.68, ("impulse_and_continuation")

    return TradingMode.UNCLASSIFIED.value, 0.0, ("mode_not_proven",)


def classify_entry_behavior(*, setup_type: str = "", regime: str = "UNKNOWN",
                            sweep_confirmed: bool = False, mss_confirmed: bool = False,
                            bos_level: Any = None, causal_zone_valid: bool = False,
                            displacement_atr: Any = None, continuation_probability: Any = None,
                            volatility_atr: Any = None) -> Tuple[str, float, Tuple[str, ...]]:
    """Choose one immutable primary wait behavior for a setup event."""
    setup = str(setup_type or "").upper()
    bos = _num(bos_level)
    disp, cont, vol = _num(displacement_atr), _num(continuation_probability), _num(volatility_atr)

    # A BOS price alone is not proof that this setup requires a retest.
    # Require an explicit setup label, or a confirmed sweep -> MSS sequence.
    if "RETEST" in setup and (mss_confirmed or bos is not None):
        return EntryBehavior.RETEST.value, 0.82, ("break_structure_reference", "retest_required")

    if sweep_confirmed and mss_confirmed and causal_zone_valid:
        return EntryBehavior.RETEST.value, 0.88, ("sweep_then_mss", "causal_zone_retest_required")

    if causal_zone_valid and ("REVERSAL" in setup or "PULLBACK" in setup):
        return EntryBehavior.PULLBACK.value, 0.78, ("causal_ob_or_fvg", "deeper_retracement_preferred")

    if disp is not None and disp >= 1.2 and cont is not None and cont >= 0.68 and vol is not None and vol >= 0.8:
        return EntryBehavior.MICRO_PULLBACK.value, 0.74, ("strong_displacement", "continuation_pressure", "shallow_retrace_only")

    return EntryBehavior.WAIT_FOR_STRUCTURE.value, 0.0, ("entry_behavior_not_proven",)


def classify_zone_event(candles: Sequence[Dict[str, Any]], *, side: str,
                        zone_low: Any, zone_high: Any, atr: Any,
                        min_acceptance_closes: int = 2,
                        max_candles: int = 120) -> Dict[str, Any]:
    """Classify price interaction with a structural zone using closed candles only.

    candles are chronological mappings with open/high/low/close/timestamp.
    The latest candle is assumed closed by the caller; unfinished data must not
    be passed here. A wick through a boundary alone is WICK_SWEEP, not breakout.
    """
    side = str(side or "").upper()
    lo, hi, atr_v = _num(zone_low), _num(zone_high), _num(atr)
    if side not in {"BUY", "SELL"} or lo is None or hi is None or lo >= hi or atr_v is None or atr_v <= 0:
        return {"event": ZoneEvent.INSUFFICIENT_DATA.value, "confirmed": False, "reason_codes": ["invalid_inputs"]}
    rows = list(candles or [])[-max(3, int(max_candles)):]
    if len(rows) < max(3, min_acceptance_closes + 1):
        return {"event": ZoneEvent.INSUFFICIENT_DATA.value, "confirmed": False, "reason_codes": ["insufficient_closed_candles"]}
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            return {"event": ZoneEvent.INSUFFICIENT_DATA.value, "confirmed": False, "reason_codes": ["invalid_candle"]}
        vals = {k: _num(row.get(k)) for k in ("open", "high", "low", "close", "timestamp")}
        if any(v is None for v in vals.values()) or vals["high"] < max(vals["open"], vals["close"], vals["low"]) or vals["low"] > min(vals["open"], vals["close"], vals["high"]):
            return {"event": ZoneEvent.INSUFFICIENT_DATA.value, "confirmed": False, "reason_codes": ["invalid_ohlc"]}
        normalized.append(vals)
    if any(normalized[i]["timestamp"] >= normalized[i+1]["timestamp"] for i in range(len(normalized)-1)):
        return {"event": ZoneEvent.INSUFFICIENT_DATA.value, "confirmed": False, "reason_codes": ["timestamps_not_monotonic"]}

    last = normalized[-1]
    prev = normalized[-2]
    ts = int(last["timestamp"])
    width = hi - lo

    # Define the breakout boundary in the direction of the trade.
    boundary = hi if side == "BUY" else lo
    opposite = lo if side == "BUY" else hi
    close_out = last["close"] > boundary if side == "BUY" else last["close"] < boundary
    wick_out = last["high"] > boundary if side == "BUY" else last["low"] < boundary
    back_inside = last["close"] <= boundary and last["close"] >= lo if side == "BUY" else last["close"] >= boundary and last["close"] <= hi
    # A prior confirmed close outside, followed by close back inside = failed breakout.
    prior_out = prev["close"] > boundary if side == "BUY" else prev["close"] < boundary
    if prior_out and back_inside:
        return {"event": ZoneEvent.FAILED_BREAKOUT.value, "confirmed": True, "level": boundary,
                "confidence": 0.78, "reason_codes": ["prior_close_outside", "subsequent_close_reentered_zone"], "timestamp": ts}
    if wick_out and not close_out:
        return {"event": ZoneEvent.WICK_SWEEP.value, "confirmed": True, "level": boundary,
                "confidence": 0.62, "reason_codes": ["wick_cross_without_close_acceptance"], "timestamp": ts}

    # Acceptance requires multiple completed candle closes beyond boundary.
    closes_out = 0
    for row in reversed(normalized):
        beyond = row["close"] > boundary if side == "BUY" else row["close"] < boundary
        if beyond:
            closes_out += 1
        else:
            break
    required = max(2, min(5, int(min_acceptance_closes)))
    if closes_out >= required:
        # A retest must happen AFTER the first accepted close. Pre-breakout
        # candles may already trade through the level and are not retests.
        acceptance_start = len(normalized) - closes_out
        post_acceptance = normalized[acceptance_start + 1:]
        pullback_touch = any(
            (r["low"] <= boundary + 0.15 * atr_v if side == "BUY" else r["high"] >= boundary - 0.15 * atr_v)
            for r in post_acceptance
        )
        retest_holds = last["close"] > boundary if side == "BUY" else last["close"] < boundary
        if pullback_touch and retest_holds:
            return {"event": ZoneEvent.BREAKOUT_RETEST.value, "confirmed": True, "level": boundary,
                    "confidence": 0.88, "reason_codes": ["acceptance_then_retest_hold"], "timestamp": ts}
        return {"event": ZoneEvent.BREAKOUT_CONFIRMED.value, "confirmed": True, "level": boundary,
                "confidence": 0.84, "reason_codes": ["multiple_closed_closes_beyond_zone"], "timestamp": ts}
    if close_out:
        return {"event": ZoneEvent.BREAKOUT_PENDING.value, "confirmed": False, "level": boundary,
                "confidence": 0.45, "reason_codes": ["single_close_requires_acceptance"], "timestamp": ts}
    in_zone = lo <= last["close"] <= hi
    if in_zone:
        return {"event": ZoneEvent.INSIDE_ZONE.value, "confirmed": False, "level": boundary,
                "confidence": 0.35, "reason_codes": ["close_inside_reference_zone"], "timestamp": ts}
    return {"event": ZoneEvent.APPROACHING.value, "confirmed": False, "level": boundary,
            "confidence": 0.25, "reason_codes": ["no_breakout_or_retest_confirmation"], "timestamp": ts}


def build_intelligence_decision(*, regime: str = "UNKNOWN", adx: Any = None,
                                continuation_probability: Any = None,
                                liquidity_quality: str = "UNKNOWN",
                                displacement_atr: Any = None,
                                setup_type: str = "", sweep_confirmed: bool = False,
                                mss_confirmed: bool = False, causal_zone_valid: bool = False,
                                bos_level: Any = None, volatility_atr: Any = None,
                                reference_low: Any = None, reference_high: Any = None,
                                data_quality: str = "UNKNOWN") -> IntelligenceDecision:
    mode, mode_conf, mode_reasons = classify_trading_mode(
        regime=regime, adx=adx, continuation_probability=continuation_probability,
        liquidity_quality=liquidity_quality, displacement_atr=displacement_atr,
        sweep_confirmed=sweep_confirmed, mss_confirmed=mss_confirmed,
        causal_zone_valid=causal_zone_valid, data_quality=data_quality)
    behavior, behavior_conf, behavior_reasons = classify_entry_behavior(
        setup_type=setup_type, regime=regime, sweep_confirmed=sweep_confirmed,
        mss_confirmed=mss_confirmed, bos_level=bos_level,
        causal_zone_valid=causal_zone_valid, displacement_atr=displacement_atr,
        continuation_probability=continuation_probability, volatility_atr=volatility_atr)
    confidence = min(mode_conf, behavior_conf) if behavior != EntryBehavior.WAIT_FOR_STRUCTURE.value else 0.0
    return IntelligenceDecision(
        mode=mode, entry_behavior=behavior, zone_event=ZoneEvent.INSUFFICIENT_DATA.value,
        confidence=round(confidence, 3), reference_level=_num(bos_level),
        reference_low=_num(reference_low), reference_high=_num(reference_high),
        reason_codes=tuple(mode_reasons + behavior_reasons), confirmed=False,
        source_timestamp=None)


def advance_failed_breakout_confirmation(*, previous_event: str,
                                          previous_timestamp: Any,
                                          previous_streak: Any,
                                          current_event: str,
                                          current_timestamp: Any,
                                          timeframe_seconds: Any) -> Tuple[str, int]:
    """Confirm a failed breakout over two adjacent closed candles.

    A re-entry after a close outside starts the state. The next distinct
    closed candle must remain inside the reference zone. Duplicate, stale,
    gapped, or unrelated events reset the consecutive count.
    """
    prev_event = str(previous_event or "").upper()
    event = str(current_event or "").upper()
    prev_ts, ts = _num(previous_timestamp), _num(current_timestamp)
    streak_n = max(0, int(_num(previous_streak) or 0))
    timeframe = _num(timeframe_seconds)
    if event == ZoneEvent.FAILED_BREAKOUT.value:
        return ZoneEvent.FAILED_BREAKOUT.value, 1
    if (
        prev_event == ZoneEvent.FAILED_BREAKOUT.value
        and event == ZoneEvent.INSIDE_ZONE.value
        and prev_ts is not None and ts is not None
        and timeframe is not None and timeframe > 0
        and ts > prev_ts
        and (ts - prev_ts) <= timeframe * 1.5
        and streak_n >= 1
    ):
        return ZoneEvent.FAILED_BREAKOUT.value, streak_n + 1
    return event or ZoneEvent.INSUFFICIENT_DATA.value, 0
