"""Single, deterministic profit-mode policy for the GGP trade manager.

This module makes no exchange calls and never sends orders. It only classifies
trade-management mode and computes price targets; execution and fill verification
remain in the existing GGP execution layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
import uuid


# Price-move percentages (not leveraged ROE percentages).
# At 20x these correspond approximately to 5%, 10%, 20% and 60% ROE,
# before fees, funding and slippage.
RANGE_PROFIT_TARGETS_PCT = (0.25, 0.50, 1.00)
TREND_TP1_MOVE_PCT = 2.00
TREND_TP2_MOVE_PCT = 3.00
RANGE_MODE = "RANGE_SCALP"
TREND_MODE = "TREND_RIDE"
STANDARD_MODE = "TWO_STAGE_50_50"


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default



def adaptive_trail_multiplier(base_multiplier: float, continuation_probability: float, adx: float,
                              distribution_risk: float, exit_warning: int, ema_state: str = "UNKNOWN",
                              peak_drawdown_roe: float = 0.0, tp1_verified: bool = True) -> float:
    """RORO-derived adaptive runner defense; only returns a multiplier, never sends orders.

    A confirmed expanding trend gets room. Distribution, warnings and giveback
    progressively tighten the stop. It never widens an already-committed stop;
    the caller applies the existing monotonic ratchet.
    """
    mult = _number(base_multiplier, 1.5) or 1.5
    cont = _number(continuation_probability, 0.0) or 0.0
    adx_value = _number(adx, 0.0) or 0.0
    dist = _number(distribution_risk, 0.0) or 0.0
    warning = int(_number(exit_warning, 0.0) or 0)
    giveback = max(0.0, _number(peak_drawdown_roe, 0.0) or 0.0)
    if cont >= 0.78 and adx_value >= 25 and dist < 35 and warning <= 1:
        mult *= 1.20
    elif cont < 0.55 or dist >= 45 or warning >= 3:
        mult *= 0.75
    if dist >= 65 or warning >= 4:
        mult *= 0.75
    if ema_state in {"TREND_RIDE", "EMA20_PULLBACK", "EMA50_PULLBACK"} and cont >= 0.60 and dist < 45 and warning <= 2:
        mult *= 1.08
    elif ema_state == "EMA20_RETEST_PENDING" and cont >= 0.50 and dist < 45 and warning <= 2:
        mult *= 1.04
    elif ema_state in {"EMA50_BREAK_PENDING", "EMA50_BREAK_CONFIRMED"}:
        mult *= 0.82 if ema_state == "EMA50_BREAK_PENDING" else 0.65
    # Once meaningful MFE exists, reduce the amount of profit given back.
    if giveback >= 12:
        mult *= 0.80
    if giveback >= 25:
        mult *= 0.75
    if tp1_verified:
        mult *= 0.95
    return max(0.55, min(3.5, mult))


def ema_trend_context(df_closed: Any, side: str, atr: float = 0.0) -> Dict[str, Any]:
    """Classify completed-candle EMA20/EMA50 structure; no live/incomplete bar use."""
    result = {"state": "INSUFFICIENT_DATA", "ema20": None, "ema50": None,
              "close": None, "trend_aligned": False, "hold_structure": False,
              "break_confirmed": False}
    direction = str(side or "").upper()
    if direction not in {"BUY", "SELL"} or df_closed is None or len(df_closed) < 55:
        return result
    try:
        import pandas as pd
        close = pd.to_numeric(df_closed["close"], errors="coerce").dropna().astype(float)
        if len(close) < 55:
            return result
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        c, e20, e50 = float(close.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])
        p1, p2 = float(close.iloc[-2]), float(close.iloc[-3])
        aligned = e20 > e50 if direction == "BUY" else e20 < e50
        if direction == "BUY":
            if aligned and c >= e20: state = "TREND_RIDE"
            elif aligned and c >= e50: state = "EMA20_PULLBACK"
            elif aligned and c < e50: state = "EMA50_BREAK_PENDING" if p1 >= float(ema50.iloc[-2]) else "EMA50_BREAK_CONFIRMED"
            else: state = "EMA50_BREAK_PENDING" if c >= e50 else "EMA50_BREAK_CONFIRMED"
        else:
            if aligned and c <= e20: state = "TREND_RIDE"
            elif aligned and c <= e50: state = "EMA20_PULLBACK"
            elif aligned and c > e50: state = "EMA50_BREAK_PENDING" if p1 <= float(ema50.iloc[-2]) else "EMA50_BREAK_CONFIRMED"
            else: state = "EMA50_BREAK_PENDING" if c <= e50 else "EMA50_BREAK_CONFIRMED"
        result.update({"state": state, "ema20": e20, "ema50": e50, "close": c,
                       "trend_aligned": bool(aligned),
                       "hold_structure": state in {"TREND_RIDE", "EMA20_PULLBACK"},
                       "break_confirmed": state == "EMA50_BREAK_CONFIRMED"})
        return result
    except Exception:
        return result


def classify_profit_mode(context: Dict[str, Any]) -> Tuple[str, str, Tuple[str, ...]]:
    """Classify only from observed evidence; UNKNOWN never means RANGE_SCALP.

    RANGE_SCALP is deliberately gated by multiple independent signs of a weak,
    choppy or thin market. Missing data falls back to the safer 50/50 plan.
    """
    ctx = context if isinstance(context, dict) else {}
    side = str(ctx.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return STANDARD_MODE, "invalid_side_default_two_stage", ("invalid_side",)

    regime = str(ctx.get("regime") or "UNKNOWN").upper()
    state = str(ctx.get("trade_state") or "UNKNOWN").upper()
    adx = _number(ctx.get("adx"))
    adx_slope = _number(ctx.get("adx_slope"))
    continuation = _number(ctx.get("continuation_probability"))
    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    volume_ratio = _number(ctx.get("volume_ratio"))
    momentum_strength = _number(ctx.get("momentum_strength"))
    spread_pct = _number(ctx.get("spread_pct"))
    depth_usdt = _number(ctx.get("orderbook_depth_usdt"))
    liquidity_quality = str(ctx.get("liquidity_quality") or "UNKNOWN").upper()
    structure_broken = bool(ctx.get("structure_broken", False))

    di_known = plus is not None and minus is not None
    di_aligned = (plus > minus) if side == "BUY" and di_known else (minus > plus) if side == "SELL" and di_known else False
    di_spread = abs(plus - minus) if di_known else None
    regime_range = regime in {"RANGE", "RANGE_CHOP", "CHOP", "CHOPPY", "COMPRESSION"} or state in {"RANGE_CHOP", "CHOP", "COMPRESSION"}
    weak_adx = adx is not None and (adx < 18.0 or (adx < 20.0 and adx_slope is not None and adx_slope <= 0.0))
    weak_continuation = continuation is not None and continuation <= 0.48
    low_volume = volume_ratio is not None and volume_ratio <= 0.75
    very_low_volume = volume_ratio is not None and volume_ratio <= 0.60
    di_tangled = di_spread is not None and di_spread <= 3.0
    weak_momentum = momentum_strength is not None and momentum_strength < 35.0
    wide_spread = spread_pct is not None and spread_pct >= 0.15
    shallow_book = depth_usdt is not None and depth_usdt > 0 and depth_usdt < _number(ctx.get("min_orderbook_depth_usdt"), 0.0)
    explicit_thin = liquidity_quality in {"THIN", "LOW", "ILLIQUID", "POOR"}
    thin_liquidity = explicit_thin or wide_spread or shallow_book

    evidence = []
    if regime_range:
        evidence.append("range_or_chop_regime")
    if weak_adx:
        evidence.append("weak_or_falling_adx")
    if weak_continuation:
        evidence.append("weak_continuation_probability")
    if low_volume:
        evidence.append("low_relative_volume")
    if di_tangled:
        evidence.append("di_pressure_tangled")
    if weak_momentum:
        evidence.append("weak_momentum")
    if thin_liquidity:
        evidence.append("thin_liquidity_evidence")
    if structure_broken:
        evidence.append("trend_structure_broken")

    # A fresh, direction-matched EMA9/VWAP or EMA50/VWAP cross is directional
    # evidence. It must not be mislabeled as a range scalp and fully closed at
    # the first small target. It does not, by itself, prove a TREND_RIDE.
    cross_direction = str(ctx.get("vwap_cross_direction") or "").upper()
    # A cross is eligible only when its originating closed candle is recent.
    # Missing timestamps/age fail closed: they must not disable RANGE_SCALP.
    cross_age = _number(ctx.get("vwap_cross_age_seconds"))
    cross_max_age = _number(ctx.get("vwap_cross_max_age_seconds"), 2 * 900.0)
    cross_time_valid = (
        cross_age is not None and cross_age >= 0.0
        and cross_max_age is not None and cross_max_age > 0.0
        and cross_age <= cross_max_age
    )
    fresh_vwap_cross = cross_time_valid and bool(
        ctx.get("fresh_ema9_vwap_cross") or ctx.get("fresh_ema50_vwap_cross")
    )
    directional_vwap_cross = fresh_vwap_cross and (
        (side == "BUY" and cross_direction == "BULLISH")
        or (side == "SELL" and cross_direction == "BEARISH")
    )
    if directional_vwap_cross:
        evidence.append("fresh_directional_ema_vwap_cross")

    # Trend riding requires several independent, side-aligned observations.
    momentum_support = (momentum_strength is not None and momentum_strength >= 55.0) or bool(ctx.get("trend_expansion", False))
    regime_trend = regime in {"STRONG_TREND", "VERY_STRONG", "EXPANSION"} or state in {"TREND_RIDE", "EXPANSION", "HEALTHY_PULLBACK"}
    confirmed_trend = (
        not structure_broken
        and regime_trend
        and adx is not None and adx >= 22.0
        and adx_slope is not None and adx_slope >= -1.5
        and continuation is not None and continuation >= 0.62
        and di_aligned
        and momentum_support
    )
    if confirmed_trend:
        return TREND_MODE, "confirmed_directional_regime_and_continuation", tuple(evidence + ["directional_di_alignment"])

    weakness_count = sum((weak_adx, weak_continuation, low_volume, di_tangled, weak_momentum, thin_liquidity, structure_broken))
    # RANGE_SCALP requires an identifiable range/weak regime plus more than one
    # supporting weakness, OR explicit thin-liquidity evidence plus weak trend data.
    range_confirmed = regime_range and weakness_count >= 2 and adx is not None and continuation is not None
    weak_market_confirmed = (
        weak_adx and weak_continuation and (low_volume or di_tangled or thin_liquidity)
    )
    thin_market_confirmed = thin_liquidity and (weak_adx or weak_continuation or low_volume) and weakness_count >= 2
    if (range_confirmed or weak_market_confirmed or thin_market_confirmed) and not directional_vwap_cross:
        return RANGE_MODE, "confirmed_range_weak_market_or_thin_liquidity", tuple(evidence)

    # A fresh directional VWAP cross vetoes the one-shot scalp classification
    # until the market proves the new move has failed. Keep TP1/TP2 50/50.
    if directional_vwap_cross:
        return STANDARD_MODE, "fresh_directional_vwap_cross_keep_two_stage_50_50", tuple(evidence)

    # Crucial fail-safe: indicator/API gaps do not automatically force a short
    # scalp or a full-position exit. Keep the canonical 50/50 management model.
    return STANDARD_MODE, "range_conditions_not_proven_keep_two_stage_50_50", tuple(evidence or ("insufficient_range_evidence",))


def range_target_price(side: str, entry_price: float, target_move_pct: float) -> float:
    side = str(side or "").upper()
    entry = _number(entry_price, 0.0) or 0.0
    move = _number(target_move_pct, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or entry <= 0 or move <= 0:
        return 0.0
    factor = move / 100.0
    target = entry * (1.0 + factor) if side == "BUY" else entry * (1.0 - factor)
    return round(target, 12)


def trend_target_prices(side: str, entry_price: float) -> Tuple[float, float]:
    """Return the fixed two-stage trend targets: TP1 +2%, TP2 +3% price move.

    These are price movement percentages, not leveraged ROE. The execution
    layer remains responsible for quantity, reduce-only orders and fill checks.
    """
    direction = str(side or "").upper()
    entry = _number(entry_price, 0.0) or 0.0
    if direction not in {"BUY", "SELL"} or entry <= 0:
        return 0.0, 0.0
    sign = 1.0 if direction == "BUY" else -1.0
    tp1 = entry * (1.0 + sign * TREND_TP1_MOVE_PCT / 100.0)
    tp2 = entry * (1.0 + sign * TREND_TP2_MOVE_PCT / 100.0)
    return round(tp1, 12), round(tp2, 12)


def target_reached(side: str, current_price: float, target_price: float) -> bool:
    side = str(side or "").upper()
    current = _number(current_price, 0.0) or 0.0
    target = _number(target_price, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or current <= 0 or target <= 0:
        return False
    return current >= target if side == "BUY" else current <= target


def select_range_target_pct(context: Dict[str, Any]) -> Tuple[float, str]:
    """Select a 0.25/0.50/1.00% PRICE move from simple, observable evidence.

    Targets are price movement percentages, not leveraged ROE. Weak/thin
    conditions stay at 0.25%; moderate follow-through earns 0.50%; strong
    directional evidence earns 1.00%. Larger directional moves belong to the
    existing trend-mode TP2 path, not the range-scalp target selector.
    """
    ctx = context if isinstance(context, dict) else {}
    adx = _number(ctx.get("adx"), 0.0) or 0.0
    continuation = _number(ctx.get("continuation_probability"), 0.0) or 0.0
    volume_ratio = _number(ctx.get("volume_ratio"), 0.0) or 0.0
    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    side = str(ctx.get("side") or "").upper()
    low_liquidity = bool(ctx.get("thin_liquidity", False)) or volume_ratio <= 0.75
    if low_liquidity or adx < 18.0 or continuation < 0.45:
        return RANGE_PROFIT_TARGETS_PCT[0], "weak_or_thin_market_conservative_0.25pct"

    di_known = plus is not None and minus is not None
    di_aligned = ((plus > minus) if side == "BUY" else (minus > plus)) if di_known and side in {"BUY", "SELL"} else False
    aligned_count = int(_number(ctx.get("aligned_closed_candles"), 0.0) or 0)
    adverse_rejection = bool(ctx.get("adverse_rejection", False))
    if (aligned_count >= 2 and continuation >= 0.72 and adx >= 22.0
            and volume_ratio >= 1.20 and di_aligned and not adverse_rejection):
        return RANGE_PROFIT_TARGETS_PCT[2], "strong_closed_candle_follow_through_1.00pct"
    if (aligned_count >= 2 and continuation >= 0.55 and adx >= 18.0
            and volume_ratio >= 0.85 and di_aligned and not adverse_rejection):
        return RANGE_PROFIT_TARGETS_PCT[1], "moderate_follow_through_0.50pct"
    return RANGE_PROFIT_TARGETS_PCT[0], "weak_follow_through_conservative_0.25pct"


def tp2_reassessment(side: str, current_price: float, tp2_price: float, atr: float,
                     continuation_probability: float, adx: float, adx_slope: float,
                     structure_aligned: bool, max_atr_distance: float = 3.0,
                     min_continuation: float = 0.45) -> Tuple[str, str]:
    side = str(side or "").upper()
    current = _number(current_price, 0.0) or 0.0
    target = _number(tp2_price, 0.0) or 0.0
    atr_value = _number(atr, 0.0) or 0.0
    continuation = _number(continuation_probability, 0.0) or 0.0
    adx_value = _number(adx, 0.0) or 0.0
    slope = _number(adx_slope, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or current <= 0 or target <= 0 or atr_value <= 0:
        return "HOLD", "insufficient_tp2_evidence"
    distance = target - current if side == "BUY" else current - target
    if distance <= 0:
        return "HOLD", "target_reached"
    remote = distance / atr_value > max_atr_distance
    weakening = continuation < min_continuation or (adx_value < 18 and slope <= 0)
    if remote and weakening:
        return "CLOSE", "tp2_remote_and_continuation_weak"
    if remote and not structure_aligned:
        return "CLOSE", "tp2_remote_and_structure_lost"
    return "HOLD", "runner_structure_and_continuation_acceptable"


def thesis_failure_action(side: str, roe_pct: float, failure_score: float,
                          failure_reasons: Iterable[str], structural_invalidation: bool = False) -> Tuple[str, str]:
    """Return EXIT/PROTECT/HOLD from independent thesis-failure evidence.

    One weak signal does not force liquidation. A structural invalidation, a very
    high failure score, or two independent severe signals at a failed-thesis score
    are required for a strict exit. A positive trade with a moderate warning is
    protected at breakeven rather than force-closed.
    """
    direction = str(side or "").upper()
    reasons = {str(item).lower() for item in (failure_reasons or [])}
    roe = _number(roe_pct, 0.0) or 0.0
    score = _number(failure_score, 0.0) or 0.0
    opposite_candle = "strong_bearish_candle" in reasons if direction == "BUY" else "strong_bullish_candle" in reasons
    di_flip = "di_flip_bearish" in reasons if direction == "BUY" else "di_flip_bullish" in reasons
    severe_count = sum((
        "strong_reclaim" in reasons,
        di_flip,
        opposite_candle,
        "low_continuation_pressure" in reasons,
    ))
    if structural_invalidation:
        return "EXIT", "structural_invalidation"
    if score >= 70.0:
        return "EXIT", f"confirmed_thesis_failure_{score:.0f}"
    if score >= 50.0 and severe_count >= 2:
        return "EXIT", "multi_evidence_thesis_failure:" + ",".join(sorted(reasons))
    if score >= 50.0 and roe > 0.0:
        return "PROTECT", "soft_thesis_warning_protect_positive_trade"
    return "HOLD", "thesis_failure_not_confirmed"


def adaptive_range_target_price(side: str, entry_price: float, atr: float,
                                swing_levels: Iterable[float] = (),
                                min_atr: float = 0.55, max_atr: float = 2.50,
                                pool_buffer_atr: float = 0.10) -> Tuple[float, str]:
    """Choose a range target from the nearest opposing swing-liquidity pool.

    Only levels beyond entry are considered. A target is placed just before the
    pool; if no credible nearby pool exists, use a conservative ATR fallback.
    This is a price target planner only; it never submits or modifies orders.
    """
    direction = str(side or "").upper()
    entry = _number(entry_price, 0.0) or 0.0
    volatility = _number(atr, 0.0) or 0.0
    if direction not in {"BUY", "SELL"} or entry <= 0 or volatility <= 0:
        return 0.0, "adaptive_target_insufficient_data"
    levels = []
    for raw in swing_levels or ():
        level = _number(raw)
        if level is None or level <= 0:
            continue
        distance = (level - entry) if direction == "BUY" else (entry - level)
        if min_atr * volatility <= distance <= max_atr * volatility:
            levels.append((distance, level))
    if levels:
        distance, level = min(levels, key=lambda item: item[0])
        buffer = min(pool_buffer_atr * volatility, distance * 0.20)
        target = level - buffer if direction == "BUY" else level + buffer
        if (direction == "BUY" and target > entry) or (direction == "SELL" and target < entry):
            return round(target, 12), "nearest_opposing_swing_liquidity_pool"
    fallback_distance = 1.10 * volatility
    target = entry + fallback_distance if direction == "BUY" else entry - fallback_distance
    return round(target, 12), "atr_fallback_no_credible_nearby_liquidity"


def evaluate_exit_risk(context: Dict[str, Any]) -> Dict[str, Any]:
    """Fuse independent, side-aware trade-management evidence into a risk state.

    This is a decision aid, not an order executor. A normal profit-lock requires
    multiple independent evidence groups. Structural invalidation and explicit
    panic states remain hard exits. Missing evidence contributes zero risk.
    """
    ctx = context if isinstance(context, dict) else {}
    side = str(ctx.get("side") or "").upper()
    roe = _number(ctx.get("roe"), 0.0) or 0.0
    if side not in {"BUY", "SELL"}:
        return {"action": "HOLD", "score": 0, "groups": (), "reasons": ("invalid_side",)}

    smart = ctx.get("smart_money") if isinstance(ctx.get("smart_money"), dict) else {}
    momentum = ctx.get("momentum") if isinstance(ctx.get("momentum"), dict) else {}
    mi = ctx.get("market_intelligence") if isinstance(ctx.get("market_intelligence"), dict) else {}
    liquidity = ctx.get("liquidity") if isinstance(ctx.get("liquidity"), dict) else {}
    ob = ctx.get("entry_order_block") if isinstance(ctx.get("entry_order_block"), dict) else {}
    fvg = ctx.get("entry_fvg") if isinstance(ctx.get("entry_fvg"), dict) else {}

    score = 0
    groups = set()
    reasons = []
    trade_state = str(ctx.get("trade_state") or "").upper()
    hard_states = {"PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION"}
    structural = bool(ctx.get("structural_invalidation") or ctx.get("structure_broken"))
    if structural:
        score += 5
        groups.add("STRUCTURE")
        reasons.append("structural_invalidation")
    if trade_state in hard_states:
        score += 5
        groups.add("STATE_MACHINE")
        reasons.append("hard_exit_state:" + trade_state)

    failure_score = _number(ctx.get("thesis_failure_score"), 0.0) or 0.0
    if failure_score >= 70:
        score += 5
        groups.add("THESIS")
        reasons.append("thesis_failure_score_70_plus")

    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    di_opposed = (
        plus is not None and minus is not None
        and ((side == "BUY" and minus > plus) or (side == "SELL" and plus > minus))
    )
    flow_bias = str(momentum.get("flow_bias") or ctx.get("flow_bias") or "").upper()
    flow_opposed = flow_bias in ({"SELL"} if side == "BUY" else {"BUY"})
    distribution = _number(smart.get("distribution_risk", ctx.get("distribution_risk")), 0.0) or 0.0
    smart_bias = str(smart.get("institutional_bias") or "").upper()
    smart_flow_opposed = smart_bias in ({"SELL"} if side == "BUY" else {"BUY"})
    exhaustion = _number(momentum.get("exhaustion_risk", ctx.get("exhaustion_risk")), 0.0) or 0.0
    climax = _number(momentum.get("climax_risk", ctx.get("climax_risk")), 0.0) or 0.0
    momentum_decay = bool(momentum.get("momentum_decay", ctx.get("momentum_decay", False)))
    if di_opposed or flow_opposed:
        score += 1 + int(di_opposed and flow_opposed)
        groups.add("DIRECTIONAL_FLOW")
        if di_opposed: reasons.append("di_pressure_opposed")
        if flow_opposed: reasons.append("momentum_flow_opposed")
    if smart_flow_opposed:
        score += 1
        groups.add("INSTITUTIONAL_FLOW")
        reasons.append("institutional_bias_opposed")
    # Distribution pressure is adverse to longs; for shorts the side-aligned
    # institutional SELL bias is continuation evidence, not an exit warning.
    distribution_adverse = distribution >= 60 and side == "BUY"
    if distribution_adverse:
        score += 1
        groups.add("INSTITUTIONAL_FLOW")
        reasons.append("distribution_risk_high")
    rsi_value = _number(momentum.get("rsi_value", ctx.get("rsi_value")))
    adverse_climax = (
        (side == "BUY" and ((rsi_value is not None and rsi_value >= 75) or climax >= 60))
        or (side == "SELL" and ((rsi_value is not None and rsi_value <= 25) or (climax >= 60 and rsi_value is None)))
    )
    if exhaustion >= 60 or adverse_climax or momentum_decay:
        score += 1
        groups.add("MOMENTUM")
        if exhaustion >= 60: reasons.append("momentum_exhaustion_high")
        if adverse_climax: reasons.append("directional_momentum_climax")
        if momentum_decay: reasons.append("momentum_decay")

    continuation = _number(ctx.get("continuation_probability"))
    counter = _number(ctx.get("counter_pressure"))
    reclaim = _number(ctx.get("reclaim_risk"))
    if ((continuation is not None and continuation <= 0.40)
            or (counter is not None and counter >= 0.65)
            or (reclaim is not None and reclaim >= 0.65)):
        score += 1
        groups.add("CONTINUATION")
        reasons.append("continuation_or_counter_pressure_deteriorated")

    cross_direction = str(ctx.get("vwap_cross_direction") or "").upper()
    cross_age = _number(ctx.get("vwap_cross_age_seconds"))
    cross_max_age = _number(ctx.get("vwap_cross_max_age_seconds"))
    cross_fresh = (
        bool(ctx.get("fresh_ema9_vwap_cross") or ctx.get("fresh_ema50_vwap_cross"))
        and cross_age is not None and cross_age >= 0
        and cross_max_age is not None and cross_max_age > 0
        and cross_age <= cross_max_age
    )
    expected_cross = "BULLISH" if side == "BUY" else "BEARISH"
    if cross_fresh and cross_direction and cross_direction != expected_cross:
        score += 2
        groups.add("CROSS_STRUCTURE")
        reasons.append("fresh_opposite_ema_vwap_cross")

    ema_state = str(ctx.get("ema_state") or "").upper()
    if ema_state == "EMA50_BREAK_CONFIRMED":
        score += 2
        groups.add("TREND_STRUCTURE")
        reasons.append("ema50_break_confirmed")
    elif ema_state == "EMA50_BREAK_PENDING":
        score += 1
        groups.add("TREND_STRUCTURE")
        reasons.append("ema50_break_pending")

    zone_event = str(mi.get("zone_event") or ctx.get("zone_event") or "").upper()
    opposite_zone_events = {"FAILED_BREAKOUT", "BREAKOUT_CONFIRMED_AGAINST_TRADE", "STRUCTURE_INVALIDATED"}
    if zone_event in opposite_zone_events:
        score += 2
        groups.add("ZONE_STRUCTURE")
        reasons.append("opposing_zone_event:" + zone_event)
    ob_validation = ob.get("validation") if isinstance(ob.get("validation"), dict) else {}
    ob_status = str(ob.get("status") or ob_validation.get("status") or "").upper()
    if ob_status in {"INVALIDATED", "MITIGATED"}:
        score += 1
        groups.add("INSTITUTIONAL_ZONE")
        reasons.append("entry_order_block_" + ob_status.lower())
    fvg_status = str(fvg.get("status") or "").upper()
    if fvg_status == "FILLED":
        score += 1
        groups.add("INSTITUTIONAL_ZONE")
        reasons.append("entry_fvg_filled")

    liq_event = str(liquidity.get("event") or "").upper()
    liq_direction = str(liquidity.get("direction") or "").upper()
    expected = "BULLISH" if side == "BUY" else "BEARISH"
    if liq_event in {"SWEEP", "GRAB"} and liq_direction not in {"", "UNKNOWN", expected}:
        score += 2
        groups.add("LIQUIDITY")
        reasons.append("opposing_liquidity_sweep")
    sr_rejection = bool(ctx.get("opposing_zone_rejection"))
    sr_distance = _number(ctx.get("opposing_zone_distance_atr"))
    if sr_rejection and (sr_distance is None or sr_distance <= 0.5):
        score += 2
        groups.add("SUPPORT_RESISTANCE")
        reasons.append("confirmed_rejection_at_opposing_sr")

    hard_exit = structural or trade_state in hard_states or failure_score >= 70
    group_count = len(groups)
    if hard_exit:
        action = "HARD_EXIT"
    elif roe > 0 and score >= 4 and group_count >= 2:
        action = "CLOSE_PROFIT"
    elif roe > 0 and score >= 2 and group_count >= 2:
        action = "LOCK_PROFIT"
    else:
        action = "HOLD"
    return {
        "action": action, "score": score, "group_count": group_count,
        "groups": tuple(sorted(groups)), "reasons": tuple(reasons),
        "roe": roe, "hard_exit": hard_exit,
    }


# ===========================================================================
# Unified Intelligent Profit Management Brain
# ===========================================================================
# This section is pure policy: it never touches the exchange, never submits or
# modifies an order, and never authorizes an entry. The existing
# TradeManagementBoard remains the sole routine authority; it delegates to this
# policy and maps the result back to its existing TradeDecision execution path.


class TradeMode(str, Enum):
    SCALP_MODE = "SCALP_MODE"
    MOMENTUM_MODE = "MOMENTUM_MODE"
    EXTENDED_MODE = "EXTENDED_MODE"
    TREND_RIDING_MODE = "TREND_RIDING_MODE"
    DEFENSIVE_MODE = "DEFENSIVE_MODE"


class BrainAction(str, Enum):
    NO_ACTION = "NO_ACTION"
    HOLD = "HOLD"
    MOVE_SL = "MOVE_SL"
    BREAKEVEN = "BREAKEVEN"
    LOCK_PROFITS = "LOCK_PROFITS"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    TRAIL = "TRAIL"
    FULL_CLOSE = "FULL_CLOSE"
    THESIS_FAILURE_EXIT = "THESIS_FAILURE_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


@dataclass
class ProfitBrainDecision:
    decision_id: str
    created_at: float
    symbol: str
    side: str
    mode: str
    mode_candidate: str
    execution_plan: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence_for: Tuple[str, ...] = ()
    evidence_against: Tuple[str, ...] = ()
    missing_evidence: Tuple[str, ...] = ()
    confidence: float = 0.0
    data_quality: str = "UNKNOWN"
    next_action: str = "none"
    confirmed_danger: bool = False
    thesis_snapshot: Dict[str, Any] = field(default_factory=dict)
    vwap_ema_context: Dict[str, Any] = field(default_factory=dict)
    entry_authorized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "created_at": self.created_at,
            "symbol": self.symbol,
            "side": self.side,
            "mode": self.mode,
            "mode_candidate": self.mode_candidate,
            "execution_plan": self.execution_plan,
            "action": self.action,
            "params": dict(self.params),
            "reason": self.reason,
            "evidence_for": list(self.evidence_for),
            "evidence_against": list(self.evidence_against),
            "missing_evidence": list(self.missing_evidence),
            "confidence": self.confidence,
            "data_quality": self.data_quality,
            "next_action": self.next_action,
            "confirmed_danger": self.confirmed_danger,
            "thesis_snapshot": dict(self.thesis_snapshot),
            "vwap_ema_context": dict(self.vwap_ema_context),
            "entry_authorized": False,
        }


def _data_quality(context: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Classify data sufficiency separately from trade risk.

    GOOD          -> analytical decisions are allowed.
    UNKNOWN_DATA  -> not enough trusted evidence for an analytical decision.
    INVALID       -> stale/invalid/insufficient market data.
    UNKNOWN_DATA is not DEFENSIVE_MODE and must never be treated as confirmed
    danger. It only disables indicator-driven protection changes.
    """
    ctx = context if isinstance(context, dict) else {}
    freshness = str(ctx.get("data_freshness") or "UNKNOWN").upper()
    if freshness in {"STALE", "INVALID", "INSUFFICIENT"}:
        return "INVALID", [f"market_data:{freshness}"]
    missing: List[str] = []
    for name in ("adx", "di_plus", "di_minus", "continuation_probability", "atr", "entry", "current_price"):
        if _number(ctx.get(name)) is None:
            missing.append(name)
    regime = str(ctx.get("regime") or "UNKNOWN").upper()
    if regime == "UNKNOWN":
        missing.append("regime")
    if not bool(ctx.get("indicator_decisions_allowed", True)):
        missing.insert(0, "indicator_decisions_disabled")
    if missing:
        return "UNKNOWN_DATA", missing
    return "GOOD", []


def build_trade_thesis_snapshot(context: Dict[str, Any]) -> Dict[str, Any]:
    """Build the entry-thesis snapshot from STATE/context evidence only.

    Missing fields are explicitly marked UNKNOWN. This function never mutates
    STATE and never fabricates entry context that the caller did not provide.
    """
    ctx = context if isinstance(context, dict) else {}
    thesis = ctx.get("thesis") if isinstance(ctx.get("thesis"), dict) else {}
    market_context = thesis.get("market_context") if isinstance(thesis.get("market_context"), dict) else {}
    gate = ctx.get("entry_gate_snapshot") if isinstance(ctx.get("entry_gate_snapshot"), dict) else {}
    entry = _number(ctx.get("entry"))
    entry_atr = _number(ctx.get("entry_atr"))
    entry_ema9 = _number(gate.get("ema9"))
    entry_ema15 = _number(gate.get("ema15"))
    entry_vwap = _number(gate.get("vwap"))
    entry_extension = None
    if entry is not None and entry_ema9 is not None and entry_atr is not None and entry_atr > 0:
        entry_extension = abs(entry - entry_ema9) / entry_atr
    snapshot: Dict[str, Any] = {
        "thesis_id": thesis.get("thesis_id") or "UNKNOWN",
        "symbol": ctx.get("symbol") or "UNKNOWN",
        "side": ctx.get("side") or "UNKNOWN",
        "entry_price": entry if entry is not None else "UNKNOWN",
        "entry_atr": entry_atr if entry_atr is not None else "UNKNOWN",
        "entry_time": ctx.get("entry_time") or "UNKNOWN",
        "entry_reason": list(thesis.get("entry_reason") or ctx.get("entry_reasons") or []),
        "entry_mode": ctx.get("entry_mode") or "UNKNOWN",
        "entry_behavior": ctx.get("entry_behavior") or "UNKNOWN",
        "entry_behavior_confidence": _number(ctx.get("entry_mode_confidence")),
        "entry_regime": market_context.get("regime") or "UNKNOWN",
        "entry_ema9": entry_ema9 if entry_ema9 is not None else "UNKNOWN",
        "entry_ema15": entry_ema15 if entry_ema15 is not None else "UNKNOWN",
        "entry_vwap": entry_vwap if entry_vwap is not None else "UNKNOWN",
        "entry_extension_atr": entry_extension if entry_extension is not None else "UNKNOWN",
        "entry_zone_low": _number(ctx.get("entry_zone_low")),
        "entry_zone_high": _number(ctx.get("entry_zone_high")),
        "entry_order_block": ctx.get("entry_order_block") or "UNKNOWN",
        "entry_fvg": ctx.get("entry_fvg") or "UNKNOWN",
        "entry_liquidity": ctx.get("entry_liquidity") or "UNKNOWN",
        "entry_golden_zone": ctx.get("entry_golden_zone") or "UNKNOWN",
        "entry_spread_pct": _number(ctx.get("entry_spread_pct")),
        "entry_liquidity_quality": ctx.get("liquidity_quality") or "UNKNOWN",
        "entry_orderbook_depth_usdt": _number(ctx.get("orderbook_depth_usdt")),
        "signal_candle_id": gate.get("signal_candle_id") or ctx.get("signal_candle_id") or "UNKNOWN",
        "entry_gate_timeframe": gate.get("candle_timeframe") or ctx.get("entry_gate_timeframe") or "UNKNOWN",
        "entry_cross_direction": gate.get("cross_direction") or "UNKNOWN",
        "entry_pullback_timestamp": gate.get("pullback_timestamp") or "UNKNOWN",
        "entry_signal_candle_timestamp": gate.get("signal_candle_timestamp") or "UNKNOWN",
    }
    unknown = []
    for key, value in snapshot.items():
        if value is None or value == "" or value == "UNKNOWN":
            unknown.append(key)
    snapshot["unknown_fields"] = unknown
    return snapshot


def _confirmed_failed_breakout(context: Dict[str, Any], side: str) -> bool:
    """Match the existing confirmed failed-breakout evidence gate exactly."""
    mi = context.get("market_intelligence") if isinstance(context.get("market_intelligence"), dict) else {}
    confidence = _number(mi.get("zone_event_confidence"))
    streak = _number(mi.get("failed_breakout_streak"))
    expected_side = str(side or "").upper()
    return bool(
        expected_side in {"BUY", "SELL"}
        and str(mi.get("zone_event") or "").upper() == "FAILED_BREAKOUT"
        and mi.get("zone_event_confirmed") is True
        and mi.get("zone_event_fresh") is True
        and str(mi.get("zone_side") or "").upper() == expected_side
        and confidence is not None and confidence >= 0.75
        and streak is not None and int(streak) >= 2
        and not bool(mi.get("failed_breakout_thesis_credit_applied", False))
    )


def classify_trade_mode(context: Dict[str, Any]) -> Tuple[TradeMode, str, Tuple[str, ...], bool]:
    """Classify the five-mode management state from multi-source evidence.

    Hysteresis is applied by the Board, not here. This function returns the
    candidate mode and whether confirmed danger requires immediate DEFENSIVE.
    """
    ctx = context if isinstance(context, dict) else {}
    side = str(ctx.get("side") or "").upper()
    plan_mode, plan_reason, plan_evidence = classify_profit_mode(ctx)
    evidence = list(plan_evidence)
    trade_state = str(ctx.get("trade_state") or "UNKNOWN").upper()
    regime = str(ctx.get("regime") or "UNKNOWN").upper()
    adx = _number(ctx.get("adx"))
    adx_slope = _number(ctx.get("adx_slope"))
    continuation = _number(ctx.get("continuation_probability"))
    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    volume_ratio = _number(ctx.get("volume_ratio"))
    momentum_strength = _number(ctx.get("momentum_strength"))
    structure_broken = bool(ctx.get("structure_broken") or ctx.get("structural_invalidation"))
    failure_score = _number(ctx.get("thesis_failure_score"), 0.0) or 0.0
    roe = _number(ctx.get("roe"), 0.0) or 0.0
    failure_reasons = list(ctx.get("thesis_failure_reasons") or [])
    if _confirmed_failed_breakout(ctx, side):
        failure_score = min(100.0, failure_score + 18.0)
        failure_reasons.append("confirmed_failed_breakout_reentry")
        evidence.append("confirmed_failed_breakout_reentry")

    exit_risk = evaluate_exit_risk(ctx)
    thesis_action, thesis_reason = thesis_failure_action(
        side, roe, failure_score, failure_reasons,
        structural_invalidation=bool(ctx.get("structural_invalidation")),
    )
    hard_states = {"PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION"}
    confirmed_danger = bool(
        ctx.get("structural_invalidation")
        or ctx.get("structure_broken")
        or trade_state in hard_states
        or failure_score >= 70.0
        or exit_risk.get("action") == "HARD_EXIT"
        or thesis_action == "EXIT"
    )
    if confirmed_danger:
        danger_reasons = list(exit_risk.get("reasons") or []) + [thesis_reason]
        return (
            TradeMode.DEFENSIVE_MODE,
            "confirmed_danger:" + ",".join(dict.fromkeys(r for r in danger_reasons if r)),
            tuple(dict.fromkeys(evidence + ["confirmed_danger"])),
            True,
        )

    defensive_soft = bool(
        exit_risk.get("action") in {"CLOSE_PROFIT", "LOCK_PROFIT"}
        or thesis_action == "PROTECT"
        or trade_state in {"DISTRIBUTION", "PROFIT_DEFENSE", "EXHAUSTION"}
    )
    if defensive_soft:
        soft_reasons = list(exit_risk.get("reasons") or []) + [thesis_reason]
        return (
            TradeMode.DEFENSIVE_MODE,
            "defensive_evidence:" + ",".join(dict.fromkeys(r for r in soft_reasons if r)),
            tuple(dict.fromkeys(evidence + ["defensive_evidence"])),
            False,
        )

    di_aligned = False
    if plus is not None and minus is not None:
        di_aligned = (plus > minus) if side == "BUY" else (minus > plus) if side == "SELL" else False
    if plan_mode == TREND_MODE:
        expansion = bool(
            regime in {"EXPANSION", "STRONG_TREND", "VERY_STRONG"}
            or ctx.get("trend_expansion")
            or (volume_ratio is not None and volume_ratio >= 1.20)
        )
        if expansion and (continuation is None or continuation >= 0.62):
            return (
                TradeMode.EXTENDED_MODE,
                "trend_expansion_with_continuation",
                tuple(dict.fromkeys(evidence + ["trend_expansion"])),
                False,
            )
        return (
            TradeMode.TREND_RIDING_MODE,
            "confirmed_trend_structure",
            tuple(dict.fromkeys(evidence + ["confirmed_trend_structure"])),
            False,
        )
    if plan_mode == RANGE_MODE:
        return (
            TradeMode.SCALP_MODE,
            "confirmed_range_or_thin_market",
            tuple(dict.fromkeys(evidence)),
            False,
        )
    momentum_support = bool(
        (momentum_strength is not None and momentum_strength >= 55.0)
        or ctx.get("trend_expansion")
    )
    if (continuation is not None and continuation >= 0.55
            and adx is not None and adx >= 20.0
            and di_aligned and not structure_broken and momentum_support):
        return (
            TradeMode.MOMENTUM_MODE,
            "momentum_support_without_confirmed_trend",
            tuple(dict.fromkeys(evidence + ["momentum_support"])),
            False,
        )
    # The canonical 50/50 plan is the safe default when trend/range evidence is
    # not strong enough. It is NOT a forced scalp and never a full close.
    return (
        TradeMode.MOMENTUM_MODE,
        "standard_two_stage_default",
        tuple(dict.fromkeys(evidence + ["standard_two_stage_default"])),
        False,
    )


def _mode_trail_factor(mode: str) -> float:
    """Mode-specific trailing multipliers are not adopted without approval.

    Shadow analysis can compare hypothetical factors with trailing_comparison();
    the live policy keeps the legacy multiplier (1.0) until the comparison is
    reviewed and explicitly approved.
    """
    return 1.0


def trailing_comparison(context: Dict[str, Any], mode_factor: float = 1.0) -> Dict[str, Any]:
    """Pure analysis helper for Legacy-vs-mode trailing comparisons.

    Returns stop distance, locked profit, room for a normal pullback and an
    early-exit proxy for 0.5/0.8/1.0/1.2/1.5 ATR retracements. It never sends
    orders and never mutates STATE. The live policy does not use mode factors
    other than 1.0 until they are approved.
    """
    ctx = context if isinstance(context, dict) else {}
    side = str(ctx.get("side") or "").upper()
    entry = _number(ctx.get("entry"))
    current = _number(ctx.get("current_price"))
    atr = _number(ctx.get("atr"))
    if side not in {"BUY", "SELL"} or entry is None or current is None or atr is None or atr <= 0:
        return {"valid": False, "reason": "insufficient_trailing_evidence"}
    base_mult = _number(ctx.get("smart_trail_mult"), 1.5) or 1.5
    trail_mult = adaptive_trail_multiplier(
        base_mult * float(mode_factor),
        _number(ctx.get("continuation_probability"), 0.5) or 0.5,
        _number(ctx.get("adx"), 20.0) or 20.0,
        _number(ctx.get("distribution_risk"), 0.0) or 0.0,
        int(_number(ctx.get("exit_warning"), 0.0) or 0.0),
        str(ctx.get("ema_state") or "UNKNOWN"),
        _number(ctx.get("peak_drawdown_roe"), 0.0) or 0.0,
        tp1_verified=True,
    )
    favourable = _number(ctx.get("max_price"), entry) if side == "BUY" else _number(ctx.get("min_price"), entry)
    favourable = favourable if favourable is not None else entry
    synthetic_sl = _number(ctx.get("synthetic_sl"), 0.0) or 0.0
    previous = _number(ctx.get("trail_stop"), 0.0) or 0.0
    be_buffer = max(entry * 0.0010, atr * 0.10)
    floor_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
    raw_stop = favourable - trail_mult * atr if side == "BUY" else favourable + trail_mult * atr
    if side == "BUY":
        new_stop = max(raw_stop, floor_stop, previous)
        current_stop = max(synthetic_sl, previous)
    else:
        new_stop = min(raw_stop, floor_stop, previous if previous > 0 else float("inf"))
        current_stop = min(synthetic_sl if synthetic_sl > 0 else float("inf"),
                           previous if previous > 0 else float("inf"))
    stop_distance_atr = abs(favourable - new_stop) / atr
    locked_profit_price = (new_stop - entry) if side == "BUY" else (entry - new_stop)
    early_exit = {}
    for depth in (0.5, 0.8, 1.0, 1.2, 1.5):
        retrace_price = favourable - depth * atr if side == "BUY" else favourable + depth * atr
        early_exit[f"{depth:.1f}"] = (
            retrace_price <= new_stop if side == "BUY" else retrace_price >= new_stop
        )
    return {
        "valid": True,
        "side": side,
        "mode_factor": float(mode_factor),
        "multiplier": trail_mult,
        "favourable": favourable,
        "new_stop": new_stop,
        "floor_stop": floor_stop,
        "current_stop": current_stop,
        "stop_distance_atr": stop_distance_atr,
        "locked_profit_price": locked_profit_price,
        "locked_profit_pct": (locked_profit_price / entry * 100.0) if entry > 0 else None,
        "room_for_pullback_atr": stop_distance_atr,
        "early_exit_proxy": early_exit,
        "be_floor_respected": (
            new_stop >= floor_stop - 1e-12 if side == "BUY" else new_stop <= floor_stop + 1e-12
        ),
    }


def _next_action_for(action: str) -> str:
    return {
        BrainAction.HOLD.value: "hold",
        BrainAction.NO_ACTION.value: "none",
        BrainAction.BREAKEVEN.value: "move_synthetic_stop_to_breakeven",
        BrainAction.LOCK_PROFITS.value: "lock_profits_with_synthetic_stop",
        BrainAction.TRAIL.value: "ratchet_synthetic_trail",
        BrainAction.PARTIAL_CLOSE.value: "close_partial_tp1",
        BrainAction.FULL_CLOSE.value: "close_full",
        BrainAction.THESIS_FAILURE_EXIT.value: "exit_thesis_failure",
        BrainAction.EMERGENCY_EXIT.value: "emergency_exit",
    }.get(action, "hold")


def evaluate_profit_brain(context: Dict[str, Any]) -> ProfitBrainDecision:
    """Pure unified profit-management policy.

    The caller (TradeManagementBoard) remains the sole execution authority.
    Missing data is marked in `missing_evidence` and never replaced with fake
    positive evidence. Single indicators cannot close a trade on their own.
    """
    ctx = context if isinstance(context, dict) else {}
    decision_id = uuid.uuid4().hex
    now = time.time()
    symbol = str(ctx.get("symbol") or "UNKNOWN")
    side = str(ctx.get("side") or "").upper()
    entry = _number(ctx.get("entry"))
    current = _number(ctx.get("current_price"))
    atr = _number(ctx.get("atr"))
    roe = _number(ctx.get("roe"), 0.0) or 0.0
    remaining = _number(ctx.get("remaining_qty"), 0.0) or 0.0
    initial = _number(ctx.get("initial_qty"), remaining) or remaining
    data_quality, missing = _data_quality(ctx)
    plan_mode, plan_reason, plan_evidence = classify_profit_mode(ctx)
    current_plan = str(ctx.get("profit_mode") or "").upper()
    # Preserve the existing execution plan (and therefore TP1/TP2 50/50 or the
    # existing RANGE_SCALP single-target behavior). The five-mode Brain state is
    # an analytical/protection layer, not a silent rewrite of the TP plan.
    execution_plan = current_plan if current_plan in {RANGE_MODE, TREND_MODE, STANDARD_MODE} else plan_mode
    # Only GOOD data may produce analytical decisions or protection changes.
    # UNKNOWN_DATA/INVALID are not DEFENSIVE_MODE and are never treated as
    # confirmed danger.
    indicator_allowed = data_quality == "GOOD"

    if indicator_allowed:
        mode, mode_reason, mode_evidence, confirmed_danger = classify_trade_mode(ctx)
    else:
        current_mode = str(ctx.get("current_mode") or TradeMode.MOMENTUM_MODE.value).upper()
        if current_mode not in {item.value for item in TradeMode}:
            current_mode = TradeMode.MOMENTUM_MODE.value
        mode = TradeMode(current_mode)
        mode_reason = "data_insufficient_mode_unchanged"
        mode_evidence = ()
        confirmed_danger = False

    mode_candidate = mode
    # Keep the effective five-mode state consistent with the existing execution
    # plan. A RANGE_SCALP execution plan must not be labelled TREND_RIDING just
    # because current evidence is trend-like; the candidate is still exposed.
    if execution_plan == RANGE_MODE and mode != TradeMode.SCALP_MODE:
        mode_candidate = mode
        mode = TradeMode.SCALP_MODE
        mode_reason = f"effective_scalp_for_range_plan;candidate={mode_candidate.value}"
        mode_evidence = tuple(dict.fromkeys(list(mode_evidence) + ["mode_aligned_to_range_execution_plan"]))
    elif execution_plan == TREND_MODE and mode == TradeMode.SCALP_MODE:
        mode_candidate = mode
        mode = TradeMode.TREND_RIDING_MODE
        mode_reason = f"effective_trend_riding_for_trend_plan;candidate={mode_candidate.value}"
        mode_evidence = tuple(dict.fromkeys(list(mode_evidence) + ["mode_aligned_to_trend_execution_plan"]))
    effective_override = str(ctx.get("effective_mode_override") or "").upper()
    if effective_override in {item.value for item in TradeMode}:
        mode = TradeMode(effective_override)

    evidence_for: List[str] = list(dict.fromkeys(list(mode_evidence) + list(plan_evidence)))
    if mode_reason:
        evidence_for.append(f"mode:{mode_reason}")
    if plan_reason:
        evidence_for.append(f"plan:{plan_reason}")
    evidence_for = list(dict.fromkeys(evidence_for))
    evidence_against: List[str] = []
    missing_evidence: List[str] = list(dict.fromkeys(missing))
    thesis_snapshot = ctx.get("thesis_snapshot") if isinstance(ctx.get("thesis_snapshot"), dict) else build_trade_thesis_snapshot(ctx)
    for field_name in (thesis_snapshot.get("unknown_fields") or [])[:12]:
        missing_evidence.append(f"entry_thesis:{field_name}")

    # VWAP + EMA management context. This is evidence only; it never authorizes
    # an entry and never closes a trade on its own.
    cross_snapshot = ctx.get("vwap_cross_snapshot") if isinstance(ctx.get("vwap_cross_snapshot"), dict) else {}
    gate_snapshot = ctx.get("entry_gate_snapshot") if isinstance(ctx.get("entry_gate_snapshot"), dict) else {}
    ema9 = _number(cross_snapshot.get("ema9"))
    if ema9 is None:
        ema9 = _number(gate_snapshot.get("ema9"))
    ema15 = _number(gate_snapshot.get("ema15"))
    vwap = _number(cross_snapshot.get("vwap"))
    if vwap is None:
        vwap = _number(gate_snapshot.get("vwap"))
    cross_state = str(cross_snapshot.get("state") or "UNKNOWN")
    cross_direction = str(cross_snapshot.get("cross_any") or cross_snapshot.get("signal") or "UNKNOWN")
    fresh_cross = bool(ctx.get("fresh_ema9_vwap_cross") or ctx.get("fresh_ema50_vwap_cross"))
    price_vwap_atr = None
    ema9_vwap_atr = None
    ema9_ema15_atr = None
    if atr and atr > 0:
        if current is not None and vwap is not None:
            price_vwap_atr = abs(current - vwap) / atr
        if ema9 is not None and vwap is not None:
            ema9_vwap_atr = abs(ema9 - vwap) / atr
        if ema9 is not None and ema15 is not None:
            ema9_ema15_atr = abs(ema9 - ema15) / atr
    alignment = "UNKNOWN"
    if ema9 is not None and vwap is not None:
        if side == "BUY":
            alignment = "ALIGNED" if ema9 > vwap and (ema15 is None or ema9 > ema15) else "OPPOSED"
        elif side == "SELL":
            alignment = "ALIGNED" if ema9 < vwap and (ema15 is None or ema9 < ema15) else "OPPOSED"
    no_chase_risk = bool(price_vwap_atr is not None and price_vwap_atr > 1.5)
    vwap_ema_context = {
        "ema9": ema9,
        "ema15": ema15,
        "vwap": vwap,
        "cross_state": cross_state,
        "cross_direction": cross_direction,
        "fresh_cross": fresh_cross,
        "alignment": alignment,
        "price_vwap_atr": price_vwap_atr,
        "ema9_vwap_atr": ema9_vwap_atr,
        "ema9_ema15_atr": ema9_ema15_atr,
        "no_chase_risk": no_chase_risk,
        "source": "CLOSED_CANDLE_VWAP_EMA_MANAGEMENT_CONTEXT",
    }
    if alignment == "ALIGNED":
        evidence_for.append("vwap_ema_aligned")
    elif alignment == "OPPOSED":
        evidence_against.append("vwap_ema_opposed")
    if fresh_cross:
        evidence_for.append(f"vwap_ema_fresh_cross:{cross_direction}")
    if no_chase_risk and price_vwap_atr is not None:
        evidence_against.append(f"price_vwap_extension_atr:{price_vwap_atr:.2f}")
    if ema9_vwap_atr is not None and ema9_vwap_atr > 1.0:
        evidence_against.append(f"ema9_vwap_distance_atr:{ema9_vwap_atr:.2f}")

    def make(action: BrainAction, reason: str, params: Optional[Dict[str, Any]] = None,
             confidence: Optional[float] = None, next_action: Optional[str] = None,
             extra_for: Iterable[str] = (), extra_against: Iterable[str] = ()) -> ProfitBrainDecision:
        local_for = list(dict.fromkeys(evidence_for + list(extra_for)))
        local_against = list(dict.fromkeys(evidence_against + list(extra_against)))
        if confidence is None:
            conf = 0.55
            if data_quality == "GOOD":
                conf += 0.15
            elif data_quality == "UNKNOWN_DATA":
                conf = 0.30
            elif data_quality == "INVALID":
                conf = 0.20
            conf += min(0.20, 0.03 * len(local_for))
            conf -= min(0.20, 0.03 * len(local_against))
            confidence = max(0.0, min(1.0, conf))
        return ProfitBrainDecision(
            decision_id=decision_id,
            created_at=now,
            symbol=symbol,
            side=side,
            mode=mode.value if isinstance(mode, TradeMode) else str(mode),
            mode_candidate=(mode_candidate.value if isinstance(mode_candidate, TradeMode)
                            else str(mode_candidate)),
            execution_plan=execution_plan,
            action=action.value,
            params=dict(params or {}),
            reason=reason,
            evidence_for=tuple(local_for),
            evidence_against=tuple(local_against),
            missing_evidence=tuple(dict.fromkeys(missing_evidence)),
            confidence=float(confidence),
            data_quality=data_quality,
            next_action=next_action or _next_action_for(action.value),
            confirmed_danger=bool(confirmed_danger),
            thesis_snapshot=dict(thesis_snapshot),
            vwap_ema_context=dict(vwap_ema_context),
            entry_authorized=False,
        )

    if side not in {"BUY", "SELL"}:
        return make(BrainAction.HOLD, "invalid_side", confidence=0.0, next_action="hold")
    if ctx.get("execution_unresolved") or ctx.get("close_reconciliation_required") or ctx.get("recovery_hold"):
        return make(BrainAction.HOLD, "exchange_execution_or_recovery_unresolved", confidence=0.9)
    if ctx.get("emergency_exit"):
        return make(BrainAction.EMERGENCY_EXIT,
                    str(ctx.get("emergency_reason") or "emergency_exit"),
                    confidence=0.95)
    if current is None or entry is None or current <= 0 or entry <= 0:
        return make(BrainAction.HOLD, "missing_price_or_entry", confidence=0.2, next_action="hold")

    # ------------------------------------------------------------------
    # Indicator-driven risk and thesis evidence. Skipped entirely for
    # price-only cycles so stale indicators cannot fabricate an exit.
    # ------------------------------------------------------------------
    if indicator_allowed:
        exit_risk = evaluate_exit_risk(ctx)
        if exit_risk.get("action") == "HARD_EXIT":
            return make(
                BrainAction.THESIS_FAILURE_EXIT,
                "PROFIT_BRAIN_HARD_RISK:" + ",".join(exit_risk.get("reasons", ())),
                params={"close_reason": "PROFIT_DANGER_HARD_EXIT",
                        "risk_score": exit_risk.get("score"),
                        "risk_groups": list(exit_risk.get("groups") or [])},
                confidence=0.95,
                extra_for=["hard_risk_fusion"],
            )
        if exit_risk.get("action") == "CLOSE_PROFIT":
            return make(
                BrainAction.FULL_CLOSE,
                "PROFIT_DANGER_LOCK:" + ",".join(exit_risk.get("reasons", ())),
                params={"close_reason": "PROFIT_DANGER_LOCK",
                        "risk_score": exit_risk.get("score"),
                        "risk_groups": list(exit_risk.get("groups") or [])},
                confidence=0.9,
                extra_for=["multi_group_profit_risk"],
            )
        if exit_risk.get("action") == "LOCK_PROFIT" and atr and atr > 0:
            fee_buffer = max(entry * 0.0010, atr * 0.10)
            candidate_stop = (
                max(entry + fee_buffer, current - 0.50 * atr)
                if side == "BUY" else
                min(entry - fee_buffer, current + 0.50 * atr)
            )
            if (side == "BUY" and candidate_stop < current) or (side == "SELL" and candidate_stop > current):
                return make(
                    BrainAction.LOCK_PROFITS,
                    "PROFIT_DANGER_TIGHTEN:" + ",".join(exit_risk.get("reasons", ())),
                    params={"new_sl": candidate_stop,
                            "risk_score": exit_risk.get("score"),
                            "risk_groups": list(exit_risk.get("groups") or [])},
                    confidence=0.85,
                    extra_for=["profit_lock_evidence"],
                )

        failure_reasons = list(ctx.get("thesis_failure_reasons") or [])
        failure_score = _number(ctx.get("thesis_failure_score"), 0.0) or 0.0
        if _confirmed_failed_breakout(ctx, side):
            failure_score = min(100.0, failure_score + 18.0)
            failure_reasons.append("confirmed_failed_breakout_reentry")
            evidence_for.append("confirmed_failed_breakout_reentry")
        thesis_action, thesis_reason = thesis_failure_action(
            side, roe, failure_score, failure_reasons,
            structural_invalidation=bool(ctx.get("structural_invalidation")),
        )
        if thesis_action == "EXIT":
            return make(
                BrainAction.THESIS_FAILURE_EXIT,
                str(ctx.get("thesis_failure_reason") or thesis_reason),
                confidence=0.9,
                extra_for=["thesis_failure_confirmed"],
            )
        if thesis_action == "PROTECT" and not ctx.get("be_done") and atr and atr > 0:
            be_buffer = max(entry * 0.0010, atr * 0.10)
            be_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
            return make(
                BrainAction.BREAKEVEN,
                thesis_reason,
                params={"new_sl": be_stop},
                confidence=0.8,
                extra_for=["thesis_warning_positive_trade"],
            )
        trade_state = str(ctx.get("trade_state") or "").upper()
        if trade_state in {"PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION"}:
            return make(
                BrainAction.FULL_CLOSE,
                f"hard_exit_state_{trade_state.lower()}",
                confidence=0.9,
                extra_for=["hard_state_multi_evidence"],
            )

    # ------------------------------------------------------------------
    # Price-only protection: always active, including during data outages.
    # ------------------------------------------------------------------
    synthetic_sl = _number(ctx.get("synthetic_sl"))
    if synthetic_sl is not None and synthetic_sl > 0:
        if (side == "BUY" and current <= synthetic_sl) or (side == "SELL" and current >= synthetic_sl):
            return make(BrainAction.FULL_CLOSE, "synthetic_structural_stop_triggered",
                        confidence=0.95)
    trail_stop = _number(ctx.get("trail_stop"))
    if ctx.get("trail_active") and trail_stop is not None and trail_stop > 0:
        if (side == "BUY" and current <= trail_stop) or (side == "SELL" and current >= trail_stop):
            return make(BrainAction.FULL_CLOSE, "profit_trailing_stop_triggered",
                        confidence=0.95)

    # ------------------------------------------------------------------
    # RANGE_SCALP remains a single-target mode, exactly as the existing Board.
    # TP1/TP2 50/50 is untouched for the other execution plans.
    # ------------------------------------------------------------------
    if execution_plan == RANGE_MODE:
        scalp_status = str(ctx.get("scalp_target_status") or "UNKNOWN").upper()
        scalp_target = _number(ctx.get("scalp_target_price"), 0.0) or 0.0
        range_target = _number(ctx.get("range_target_price"), 0.0) or (
            scalp_target if scalp_status == "VALID" else 0.0
        )
        target_source = ("adaptive_price_target" if _number(ctx.get("range_target_price"), 0.0)
                         else "opposing_liquidity_fallback")
        if range_target > 0 and remaining > 0 and target_reached(side, current, range_target):
            return make(
                BrainAction.FULL_CLOSE,
                f"range_scalp_{target_source}_single_target_full_exit",
                params={"close_reason": "SCALP_TARGET",
                        "target_price": range_target,
                        "target_move_pct": _number(ctx.get("range_target_move_pct"), 0.0) or 0.0,
                        "opposing_liquidity_level": _number(ctx.get("opposing_liquidity_level"), 0.0) or 0.0},
                confidence=0.9,
                extra_for=["range_single_target_reached"],
            )
        return make(
            BrainAction.HOLD,
            "range_scalp_waiting_for_single_target",
            params={"target_price": range_target, "target_source": target_source,
                    "target_move_pct": _number(ctx.get("range_target_move_pct"), 0.0) or 0.0},
            confidence=0.6,
        )

    tp1_hit = bool(ctx.get("tp1_hit"))
    tp2_hit = bool(ctx.get("tp2_hit"))
    if not tp1_hit and remaining > 0 and initial > 0:
        tp1_price = _number(ctx.get("tp1_price", ctx.get("synthetic_tp1")), 0.0) or 0.0
        if tp1_price > 0 and target_reached(side, current, tp1_price):
            target_remaining = initial * 0.5
            tolerance = max(initial * 0.002, 1e-10)
            qty_to_close = max(0.0, remaining - target_remaining)
            if qty_to_close <= tolerance:
                return make(BrainAction.HOLD,
                            "tp1_quantity_already_at_50pct_wait_for_reconciliation",
                            confidence=0.7)
            ratio = min(1.0, qty_to_close / remaining)
            return make(
                BrainAction.PARTIAL_CLOSE,
                "TP1_FIXED_50_PERCENT_OF_ORIGINAL_FILLED_QTY",
                params={"ratio": ratio, "target_remaining_qty": target_remaining,
                        "close_reason": "TP1"},
                confidence=0.9,
                extra_for=["tp1_fixed_50_50_plan"],
            )

    if tp1_hit and not tp2_hit and remaining > 0:
        tp2_price = _number(ctx.get("tp2_price"), 0.0) or 0.0
        if indicator_allowed:
            legacy_tp2 = str(ctx.get("tp2_decision") or "HOLD")
            evidence_for.append(
                f"legacy_tp2:{legacy_tp2}:{str(ctx.get('tp2_decision_reason') or '')}"
            )
            # Mode-specific runner policy. TP1/TP2 prices and the 50/50 split are
            # untouched; only the evidence threshold for closing the remaining
            # runner early changes with the five-mode state.
            mode_tp2_thresholds = {
                TradeMode.SCALP_MODE.value: (2.0, 0.50),
                TradeMode.MOMENTUM_MODE.value: (3.0, 0.45),
                TradeMode.EXTENDED_MODE.value: (4.0, 0.55),
                TradeMode.TREND_RIDING_MODE.value: (4.5, 0.55),
                TradeMode.DEFENSIVE_MODE.value: (2.0, 0.50),
            }
            max_atr, min_continuation = mode_tp2_thresholds.get(mode.value, (3.0, 0.45))
            brain_tp2_action, brain_tp2_reason = tp2_reassessment(
                side, current, tp2_price, atr or 0.0,
                _number(ctx.get("continuation_probability"), 0.5) or 0.5,
                _number(ctx.get("adx"), 20.0) or 20.0,
                _number(ctx.get("adx_slope"), 0.0) or 0.0,
                bool(ctx.get("structure_aligned")),
                max_atr_distance=max_atr,
                min_continuation=min_continuation,
            )
            evidence_for.append(f"brain_tp2:{brain_tp2_action}:{brain_tp2_reason}")
            if brain_tp2_action == "CLOSE":
                return make(
                    BrainAction.FULL_CLOSE,
                    brain_tp2_reason,
                    params={"close_reason": "TP2_ADAPTIVE_STRICT_EXIT",
                            "mode_max_atr": max_atr,
                            "mode_min_continuation": min_continuation},
                    confidence=0.85,
                    extra_for=[f"mode_tp2_policy:{mode.value}"],
                )
        if tp2_price > 0 and target_reached(side, current, tp2_price):
            return make(
                BrainAction.FULL_CLOSE,
                "TP2_FINAL_50_PERCENT_RUNNER_TARGET",
                params={"close_reason": "TP2"},
                confidence=0.9,
                extra_for=["tp2_final_50_percent_target"],
            )

    if indicator_allowed and tp1_hit and not ctx.get("be_done") and atr and atr > 0:
        be_buffer = max(entry * 0.0010, atr * 0.10)
        be_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
        return make(BrainAction.BREAKEVEN, "TP1_verified_move_runner_to_BE_plus",
                    params={"new_sl": be_stop}, confidence=0.9,
                    extra_for=["tp1_verified_runner_protection"])

    if indicator_allowed and tp1_hit and atr and atr > 0:
        base_mult = _number(ctx.get("smart_trail_mult"), 1.5) or 1.5
        mode_factor = _mode_trail_factor(mode.value if isinstance(mode, TradeMode) else str(mode))
        trail_mult = adaptive_trail_multiplier(
            base_mult * mode_factor,
            _number(ctx.get("continuation_probability"), 0.5) or 0.5,
            _number(ctx.get("adx"), 20.0) or 20.0,
            _number(ctx.get("distribution_risk"), 0.0) or 0.0,
            int(_number(ctx.get("exit_warning"), 0.0) or 0.0),
            str(ctx.get("ema_state") or "UNKNOWN"),
            _number(ctx.get("peak_drawdown_roe"), 0.0) or 0.0,
            tp1_verified=True,
        )
        favourable = _number(ctx.get("max_price"), entry) if side == "BUY" else _number(ctx.get("min_price"), entry)
        favourable = favourable if favourable is not None else entry
        new_stop = favourable - trail_mult * atr if side == "BUY" else favourable + trail_mult * atr
        be_buffer = max(entry * 0.0010, atr * 0.10)
        floor_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
        previous = _number(ctx.get("trail_stop"), 0.0) or 0.0
        if side == "BUY":
            new_stop = max(new_stop, floor_stop, previous)
            current_stop = max(synthetic_sl or 0.0, previous)
            hit = current <= new_stop
            improves = new_stop > current_stop + max(entry * 1e-8, 1e-10)
        else:
            new_stop = min(new_stop, floor_stop, previous if previous > 0 else float("inf"))
            current_stop = min(synthetic_sl if synthetic_sl and synthetic_sl > 0 else float("inf"),
                               previous if previous > 0 else float("inf"))
            hit = current >= new_stop
            improves = new_stop < current_stop - max(entry * 1e-8, 1e-10)
        if hit:
            return make(BrainAction.FULL_CLOSE, "runner_trailing_ratchet_triggered",
                        params={"close_reason": "TRAILING_STOP"}, confidence=0.95)
        if improves:
            return make(BrainAction.TRAIL, "ratchet_runner_trailing_stop",
                        params={"new_stop": new_stop, "multiplier": trail_mult},
                        confidence=0.8,
                        extra_for=["runner_trailing_evidence"])

    return make(BrainAction.HOLD, "no_profit_management_trigger", confidence=0.5)


def compare_profit_brain_decisions(legacy: Any, brain: ProfitBrainDecision,
                                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compare the legacy Board decision with the new Brain decision (shadow)."""
    ctx = context if isinstance(context, dict) else {}
    legacy_action_raw = getattr(legacy, "action", None)
    legacy_action = getattr(legacy_action_raw, "value", legacy_action_raw)
    legacy_params = dict(getattr(legacy, "params", {}) or {})
    brain_params = dict(brain.params or {})
    keys = ("new_sl", "new_stop", "ratio", "target_remaining_qty",
            "close_reason", "target_price", "multiplier")
    differences = {
        key: {"legacy": legacy_params.get(key), "brain": brain_params.get(key)}
        for key in keys
        if legacy_params.get(key) != brain_params.get(key)
    }
    protection_actions = {"BREAKEVEN", "TRAIL", "LOCK_PROFITS", "MOVE_SL"}
    risk_flags = []
    if brain.data_quality in {"UNKNOWN_DATA", "INVALID"}:
        if str(legacy_action) in protection_actions:
            risk_flags.append("legacy_protection_change_on_invalid_data")
        if str(brain.action) in protection_actions:
            risk_flags.append("brain_protection_change_on_invalid_data")
    return {
        "match": str(legacy_action) == str(brain.action) and not differences,
        "legacy_action": str(legacy_action),
        "brain_action": str(brain.action),
        "legacy_reason": str(getattr(legacy, "reason", "") or ""),
        "brain_reason": str(brain.reason or ""),
        "legacy_mode": str(ctx.get("profit_mode") or "UNKNOWN"),
        "brain_mode": str(brain.mode),
        "brain_mode_candidate": str(brain.mode_candidate),
        "execution_plan": str(brain.execution_plan),
        "legacy_params": legacy_params,
        "brain_params": brain_params,
        "differences": differences,
        "data_quality": str(brain.data_quality),
        "confirmed_danger": bool(brain.confirmed_danger),
        "vwap_ema_context": dict(brain.vwap_ema_context),
        "risk_flags": risk_flags,
        "confidence": float(brain.confidence),
        "evidence_for": list(brain.evidence_for),
        "evidence_against": list(brain.evidence_against),
        "missing_evidence": list(brain.missing_evidence),
        "decision_id": str(brain.decision_id),
        "entry_authorized": False,
    }

