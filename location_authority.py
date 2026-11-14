"""Single institutional-location validation authority.

Pure functions only: no exchange calls, API calls, order submission, or mutable state.
All timestamps are expected in the same unit as the supplied candle timestamps.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping


def validate_location_zone(
    zone: Mapping[str, Any],
    side: str,
    reference_price: float,
    atr: float,
    latest_timestamp: int,
    timeframe_ms: int,
    *,
    max_age_bars: int = 48,
    max_distance_atr: float = 1.5,
) -> Dict[str, Any]:
    """Validate one OB/FVG/SR zone against side, quality, freshness and price distance."""
    result = {"valid": False, "reason": "invalid_location_zone"}
    try:
        direction = str(side or "").upper()
        expected_direction = "BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else ""
        expected_sr = "SUPPORT" if direction == "BUY" else "RESISTANCE" if direction == "SELL" else ""
        kind = str(zone.get("kind") or "").upper()
        low, high = float(zone.get("low")), float(zone.get("high"))
        px, volatility = float(reference_price), float(atr)
        formed_at = int(zone.get("available_timestamp", zone.get("timestamp")))
        latest = int(latest_timestamp)
        duration = int(timeframe_ms)
        if not expected_direction or kind not in {"ORDER_BLOCK", "FVG", "SUPPORT", "RESISTANCE"}:
            return {**result, "reason": "unsupported_zone_or_side"}
        if not all(math.isfinite(v) for v in (low, high, px, volatility)) or not (0 < low < high and px > 0 and volatility > 0):
            return {**result, "reason": "invalid_zone_geometry_or_market_reference"}
        if formed_at <= 0 or duration <= 0 or latest <= 0:
            return {**result, "reason": "invalid_zone_timestamp"}
        if formed_at > latest:
            return {**result, "reason": "future_dated_zone"}
        age_bars = (latest - formed_at) / duration
        if age_bars > max(0, int(max_age_bars)):
            return {**result, "reason": "stale_zone", "age_bars": age_bars}
        max_distance = float(max_distance_atr)
        if not math.isfinite(max_distance) or max_distance < 0:
            return {**result, "reason": "invalid_no_chase_threshold"}
        distance = max(low - px, 0.0, px - high) / volatility
        if not math.isfinite(distance) or distance > max_distance:
            return {**result, "reason": "zone_outside_no_chase_distance", "distance_atr": distance}
        if kind == "ORDER_BLOCK":
            definition = str(zone.get("zone_definition") or "").upper()
            expected_definition = "BULLISH_LOW_TO_OPEN" if direction == "BUY" else "BEARISH_OPEN_TO_HIGH"
            ob_direction = str(zone.get("direction") or "").upper()
            # A supplied canonical geometry definition must match exactly; a
            # contradictory direction field cannot override it.
            if definition:
                if definition != expected_definition:
                    return {**result, "reason": "order_block_direction_unconfirmed"}
            elif ob_direction != expected_direction:
                return {**result, "reason": "order_block_direction_unconfirmed"}
            strength = float(zone.get("strength", 0) or 0)
            if not math.isfinite(strength) or strength < 5.0:
                return {**result, "reason": "order_block_quality_below_threshold"}
        elif kind == "FVG":
            if str(zone.get("direction") or "").upper() != expected_direction:
                return {**result, "reason": "fvg_direction_mismatch"}
            if str(zone.get("status") or "").upper() not in {"UNFILLED", "PARTIALLY_FILLED"}:
                return {**result, "reason": "fvg_not_actionable"}
            size_atr = float(zone.get("size_atr", 0) or 0)
            if not math.isfinite(size_atr) or size_atr < 0.08:
                return {**result, "reason": "fvg_too_small"}
        else:
            if kind != expected_sr or str(zone.get("status") or "").upper() != "CONFIRMED":
                return {**result, "reason": "support_resistance_not_side_aligned_or_confirmed"}
            strength = float(zone.get("strength", 0) or 0)
            if not math.isfinite(strength) or strength < 5.0:
                return {**result, "reason": "support_resistance_quality_below_threshold"}
        return {
            "valid": True, "reason": "location_valid", "kind": kind,
            "low": low, "high": high, "distance_atr": distance,
            "age_bars": age_bars, "available_timestamp": formed_at,
        }
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return {**result, "reason": "malformed_location_zone"}
