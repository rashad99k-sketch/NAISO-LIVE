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

    if regime in {"STRONG_TREND", "VERY_STRONG", "STRONG_TRENDING", "EXPANSION", "TREND",
                  "TREND_CONTINUATION", "EMA20_PULLBACK", "EMA50_DEEP_PULLBACK"} and (
        adx_v is not None and adx_v >= 22 and cont is not None and cont >= 0.60
    ):
        return TradingMode.TREND.value, 0.82, ("directional_regime", "adx_confirmed", "continuation_confirmed")

    if regime in {"RANGE", "CHOPPY", "COMPRESSION", "RANGE_CHOP", "RANGE_OR_UNCLEAR"} and (
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
    prior_was_reentry = (
        prev["close"] <= boundary and prev["close"] >= lo
        if side == "BUY" else prev["close"] >= boundary and prev["close"] <= hi
    )
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



def select_opposing_liquidity_pool(levels, side, current_price):
    """Return nearest confirmed opposing swing pool beyond current price."""
    direction = str(side or "").strip().upper()
    current = _num(current_price)
    if direction not in {"BUY", "SELL"} or current is None or current <= 0:
        return {"status": "UNKNOWN", "level": None, "distance_pct": None}
    candidates = []
    for raw in levels or ():
        value = _num(raw)
        if value is None or value <= 0:
            continue
        distance = value - current if direction == "BUY" else current - value
        if distance > 0:
            candidates.append((distance, value))
    if not candidates:
        return {"status": "NO_OPPOSING_POOL", "level": None, "distance_pct": None}
    distance, level = min(candidates, key=lambda item: item[0])
    return {"status": "VALID", "level": float(level),
            "distance_pct": round(float(distance / current * 100.0), 6)}


def select_scalp_target_pct(opposing_distance_pct, candidates=(0.25, 0.5, 1.0, 2.0),
                            pool_buffer_pct=0.05):
    """Choose the largest configured target that remains before opposing liquidity."""
    distance = _num(opposing_distance_pct)
    buffer = _num(pool_buffer_pct)
    if distance is None or distance <= 0 or buffer is None or buffer < 0:
        return 0.0, "opposing_liquidity_unavailable"
    eligible = []
    for raw in candidates or ():
        value = _num(raw)
        if value is not None and value > 0 and value + buffer <= distance:
            eligible.append(value)
    if not eligible:
        return 0.0, "opposing_pool_too_close_for_minimum_target"
    selected = max(eligible)
    return float(selected), f"largest_target_before_opposing_pool_{selected:g}pct"


def build_scalp_target(side, entry_price, opposing_pool_level,
                          candidates=(0.25, 0.5, 1.0, 2.0), pool_buffer_pct=0.05):
    """Build a fixed entry-anchored target strictly before opposing liquidity.

    The pool is selected beyond the live mark price; target distance is then
    measured from immutable entry, so a moving mark cannot move the target away.
    """
    direction = str(side or "").strip().upper()
    entry = _num(entry_price)
    pool = _num(opposing_pool_level)
    if direction not in {"BUY", "SELL"} or entry is None or entry <= 0 or pool is None or pool <= 0:
        return {"status": "UNKNOWN", "target_price": 0.0, "target_pct": 0.0,
                "entry_distance_pct": None, "reason": "invalid_side_entry_or_pool"}
    distance = (pool - entry) if direction == "BUY" else (entry - pool)
    if distance <= 0:
        return {"status": "NO_TARGET", "target_price": 0.0, "target_pct": 0.0,
                "entry_distance_pct": round(distance / entry * 100.0, 6),
                "reason": "opposing_pool_not_beyond_entry"}
    distance_pct = distance / entry * 100.0
    selected, reason = select_scalp_target_pct(
        distance_pct, candidates=candidates, pool_buffer_pct=pool_buffer_pct
    )
    if selected < 0.5:
        return {"status": "NO_TARGET", "target_price": 0.0, "tp1_target_price": 0.0,
                "target_pct": 0.0, "tp1_target_pct": 0.0,
                "entry_distance_pct": round(distance_pct, 6),
                "reason": "insufficient_room_for_two_stage_50_50_scalp_target"}
    target = entry * (1.0 + selected / 100.0) if direction == "BUY" else entry * (1.0 - selected / 100.0)
    tp1_pct = selected / 2.0
    tp1_target = entry * (1.0 + tp1_pct / 100.0) if direction == "BUY" else entry * (1.0 - tp1_pct / 100.0)
    before_pool = target < pool if direction == "BUY" else target > pool
    if not before_pool:
        return {"status": "NO_TARGET", "target_price": 0.0, "tp1_target_price": 0.0,
                "target_pct": 0.0, "tp1_target_pct": 0.0,
                "entry_distance_pct": round(distance_pct, 6),
                "reason": "target_geometry_crosses_opposing_pool"}
    return {"status": "VALID", "target_price": round(float(target), 12),
            "tp1_target_price": round(float(tp1_target), 12),
            "target_pct": float(selected), "tp1_target_pct": float(tp1_pct),
            "entry_distance_pct": round(distance_pct, 6),
            "reason": reason + "_entry_anchored_two_stage_50_50_before_opposing_pool"}


def is_confirmed_failed_breakout_evidence(market_intelligence: Any, side: str) -> bool:
    """Return whether a fresh, same-side, two-candle failed-breakout proof is usable.

    This is an evidence gate only. It never issues or authorizes an exchange order.
    """
    if not isinstance(market_intelligence, dict):
        return False
    expected_side = str(side or "").upper()
    confidence = _num(market_intelligence.get("zone_event_confidence"))
    streak = _num(market_intelligence.get("failed_breakout_streak"))
    return bool(
        expected_side in {"BUY", "SELL"}
        and str(market_intelligence.get("zone_event") or "").upper() == ZoneEvent.FAILED_BREAKOUT.value
        and market_intelligence.get("zone_event_confirmed") is True
        and market_intelligence.get("zone_event_fresh") is True
        and str(market_intelligence.get("zone_side") or "").upper() == expected_side
        and confidence is not None and confidence >= 0.75
        and streak is not None and int(streak) >= 2
        and not bool(market_intelligence.get("failed_breakout_thesis_credit_applied", False))
    )

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
        if (
            prev_event == ZoneEvent.FAILED_BREAKOUT.value
            and prev_ts is not None and ts is not None
            and timeframe is not None and timeframe > 0
            and ts > prev_ts
            and (ts - prev_ts) <= timeframe * 1.5
            and streak_n >= 1
        ):
            return ZoneEvent.FAILED_BREAKOUT.value, streak_n + 1
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


def classify_price_pattern(candles: Sequence[Dict[str, Any]], *, atr: Any,
                           impulse_bars: int = 8, flag_bars: int = 6,
                           range_bars: int = 10) -> Dict[str, Any]:
    """Detect conservative bull/bear flags and flat-top/bottom consolidations.

    Input must contain completed candles only. This is pattern recognition and
    telemetry, not an entry authorization or order-management signal.
    """
    atr_v = _num(atr)
    # Flat-level patterns need only their range window; flags additionally
    # require the longer impulse + consolidation sequence below.
    minimum = max(range_bars + 1, 6)
    if atr_v is None or atr_v <= 0 or len(candles or ()) < minimum:
        return {"pattern": "UNKNOWN", "stage": "INSUFFICIENT_DATA",
                "direction": "NEUTRAL", "confidence": 0.0,
                "reference_level": None, "reason_codes": ["insufficient_closed_candles_or_atr"]}

    rows = []
    for row in list(candles or ()):
        if not isinstance(row, dict):
            return {"pattern": "UNKNOWN", "stage": "INVALID_DATA",
                    "direction": "NEUTRAL", "confidence": 0.0,
                    "reference_level": None, "reason_codes": ["invalid_candle"]}
        values = {key: _num(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if (
            any(value is None for value in values.values())
            or min(values["open"], values["high"], values["low"], values["close"]) <= 0
            or values["volume"] < 0
            or values["high"] < max(values["open"], values["close"], values["low"])
            or values["low"] > min(values["open"], values["close"], values["high"])
        ):
            return {"pattern": "UNKNOWN", "stage": "INVALID_DATA",
                    "direction": "NEUTRAL", "confidence": 0.0,
                    "reference_level": None, "reason_codes": ["invalid_ohlcv"]}
        rows.append(values)

    trigger = rows[-1]
    if len(rows) >= impulse_bars + flag_bars + 1:
        impulse = rows[-(flag_bars + impulse_bars + 1):-(flag_bars + 1)]
        flag = rows[-(flag_bars + 1):-1]
        if len(impulse) != impulse_bars or len(flag) != flag_bars:
            return {"pattern": "UNKNOWN", "stage": "INSUFFICIENT_DATA",
                    "direction": "NEUTRAL", "confidence": 0.0,
                    "reference_level": None, "reason_codes": ["window_not_available"]}
    
        def _median(values):
            ordered = sorted(float(v) for v in values)
            n = len(ordered)
            return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    
        impulse_volume = _median([row["volume"] for row in impulse])
        flag_volume = _median([row["volume"] for row in flag])
        volume_contracts = impulse_volume > 0 and flag_volume <= impulse_volume * 1.25
        impulse_high = max(row["high"] for row in impulse)
        impulse_low = min(row["low"] for row in impulse)
        flag_high = max(row["high"] for row in flag)
        flag_low = min(row["low"] for row in flag)
    
        # Bull flag: strong upward impulse, contained/shallower downward pause,
        # then optionally a close through the flag's own ceiling.
        bull_move = impulse[-1]["close"] - impulse[0]["open"]
        bull_retrace = (impulse_high - flag_low) / bull_move if bull_move > 0 else float("inf")
        bull_directional = sum(row["close"] > row["open"] for row in impulse) / impulse_bars
        bull_pause = flag[-1]["close"] - flag[0]["open"]
        bull_flag = (
            bull_move >= 1.8 * atr_v
            and bull_directional >= 0.625
            and 0.10 <= bull_retrace <= 0.65
            and bull_pause <= bull_move * 0.25
            and flag_low > impulse[0]["open"] - 0.15 * atr_v
            and flag_high <= impulse_high + 0.50 * atr_v
        )
        if bull_flag:
            breakout = trigger["close"] > flag_high + 0.05 * atr_v
            confidence = 0.72 + (0.08 if volume_contracts else 0.0) + (0.08 if breakout else 0.0)
            return {"pattern": "BULL_FLAG",
                    "stage": "BREAKOUT_CONFIRMED" if breakout else "FORMING",
                    "direction": "BULLISH", "confidence": round(min(0.90, confidence), 3),
                    "reference_level": round(float(flag_high), 12),
                    "reason_codes": ["upward_impulse_atr_confirmed", "contained_flag_retracement"]
                                    + (["flag_volume_contraction"] if volume_contracts else [])
                                    + (["closed_candle_flag_ceiling_break"] if breakout else ["waiting_for_flag_ceiling_break"])}
    
        # Bear flag is the exact directional mirror of the bull flag.
        bear_move = impulse[0]["open"] - impulse[-1]["close"]
        bear_retrace = (flag_high - impulse_low) / bear_move if bear_move > 0 else float("inf")
        bear_directional = sum(row["close"] < row["open"] for row in impulse) / impulse_bars
        bear_pause = flag[-1]["close"] - flag[0]["open"]
        bear_flag = (
            bear_move >= 1.8 * atr_v
            and bear_directional >= 0.625
            and 0.10 <= bear_retrace <= 0.65
            and bear_pause >= -bear_move * 0.25
            and flag_high < impulse[0]["open"] + 0.15 * atr_v
            and flag_low >= impulse_low - 0.50 * atr_v
        )
        if bear_flag:
            breakout = trigger["close"] < flag_low - 0.05 * atr_v
            confidence = 0.72 + (0.08 if volume_contracts else 0.0) + (0.08 if breakout else 0.0)
            return {"pattern": "BEAR_FLAG",
                    "stage": "BREAKOUT_CONFIRMED" if breakout else "FORMING",
                    "direction": "BEARISH", "confidence": round(min(0.90, confidence), 3),
                    "reference_level": round(float(flag_low), 12),
                    "reason_codes": ["downward_impulse_atr_confirmed", "contained_flag_retracement"]
                                    + (["flag_volume_contraction"] if volume_contracts else [])
                                    + (["closed_candle_flag_floor_break"] if breakout else ["waiting_for_flag_floor_break"])}
    
    # Flat top/bottom: repeated, independently observed boundary tests. The
    # breakout candle is evaluated separately from the consolidation window.
    base = rows[-(range_bars + 1):-1]
    tolerance = max(0.18 * atr_v, float(_median([r["close"] for r in base])) * 0.0005)
    highs = [r["high"] for r in base]
    lows = [r["low"] for r in base]
    ceiling = max(highs)
    floor = min(lows)
    top_touches = [value for value in highs if ceiling - value <= tolerance]
    bottom_touches = [value for value in lows if value - floor <= tolerance]
    split = max(1, len(base) // 2)
    older_lows = _median([r["low"] for r in base[:split]])
    newer_lows = _median([r["low"] for r in base[split:]])
    older_highs = _median([r["high"] for r in base[:split]])
    newer_highs = _median([r["high"] for r in base[split:]])
    base_range = ceiling - floor

    flat_top = (
        len(top_touches) >= 3 and base_range >= 0.8 * atr_v
        and base_range <= 5.0 * atr_v
        and newer_lows >= older_lows - 0.10 * atr_v
    )
    if flat_top:
        breakout = trigger["close"] > ceiling + 0.05 * atr_v
        return {"pattern": "FLAT_TOP",
                "stage": "BREAKOUT_CONFIRMED" if breakout else "FORMING",
                "direction": "BULLISH", "confidence": 0.82 if breakout else 0.68,
                "reference_level": round(float(ceiling), 12),
                "reason_codes": ["three_or_more_resistance_tests", "base_lows_not_collapsing"]
                                + (["closed_candle_resistance_break"] if breakout else ["waiting_for_resistance_break"])}

    flat_bottom = (
        len(bottom_touches) >= 3 and base_range >= 0.8 * atr_v
        and base_range <= 5.0 * atr_v
        and newer_highs <= older_highs + 0.10 * atr_v
    )
    if flat_bottom:
        breakout = trigger["close"] < floor - 0.05 * atr_v
        return {"pattern": "FLAT_BOTTOM",
                "stage": "BREAKOUT_CONFIRMED" if breakout else "FORMING",
                "direction": "BEARISH", "confidence": 0.82 if breakout else 0.68,
                "reference_level": round(float(floor), 12),
                "reason_codes": ["three_or_more_support_tests", "base_highs_not_rising"]
                                + (["closed_candle_support_break"] if breakout else ["waiting_for_support_break"])}

    return {"pattern": "NONE", "stage": "NO_PATTERN",
            "direction": "NEUTRAL", "confidence": 0.0,
            "reference_level": None, "reason_codes": ["no_qualified_flag_or_flat_level_pattern"]}
