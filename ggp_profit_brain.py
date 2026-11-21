"""Single, deterministic profit-mode policy for the GGP trade manager.

This module makes no exchange calls and never sends orders. It only classifies
trade-management mode and computes price targets; execution and fill verification
remain in the existing GGP execution layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Dict, Iterable, Optional, Tuple


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
