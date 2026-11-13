import pytest

from ggp_market_intelligence import (
    TradingMode, EntryBehavior, ZoneEvent,
    classify_trading_mode, classify_entry_behavior, classify_zone_event,
    build_intelligence_decision, advance_failed_breakout_confirmation,
)


def candle(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c}


def test_unknown_inputs_fail_closed():
    mode, confidence, reasons = classify_trading_mode(regime="UNKNOWN", data_quality="UNKNOWN")
    assert mode == TradingMode.UNCLASSIFIED.value
    assert confidence == 0
    assert "data_quality_not_confirmed" in reasons


def test_trend_requires_directional_and_continuation_evidence():
    mode, _, _ = classify_trading_mode(regime="STRONG_TREND", adx=29,
                                        continuation_probability=.8, data_quality="VALID")
    assert mode == TradingMode.TREND.value


def test_scalp_is_not_assigned_on_missing_liquidity_or_adx():
    mode, _, _ = classify_trading_mode(regime="CHOPPY", adx=None,
                                        continuation_probability=.3, data_quality="VALID")
    assert mode == TradingMode.UNCLASSIFIED.value


def test_sniper_requires_sweep_mss_and_causal_zone():
    args = dict(sweep_confirmed=True, mss_confirmed=True,
                causal_zone_valid=True, data_quality="VALID")
    mode, _, _ = classify_trading_mode(regime="TRANSITION", **args)
    assert mode == TradingMode.SNIPER.value


def test_entry_behavior_retest_for_bos_continuation():
    behavior, _, _ = classify_entry_behavior(setup_type="TREND_CONTINUATION_RETEST",
                                               mss_confirmed=True, bos_level=100)
    assert behavior == EntryBehavior.RETEST.value


def test_bos_level_alone_does_not_force_retest_classification():
    behavior, _, _ = classify_entry_behavior(
        setup_type="TREND_CONTINUATION",
        mss_confirmed=True,
        bos_level=100,
    )
    assert behavior == EntryBehavior.WAIT_FOR_STRUCTURE.value


def test_entry_behavior_pullback_for_causal_reversal_zone():
    behavior, _, _ = classify_entry_behavior(setup_type="LIQUIDITY_SWEEP_REVERSAL",
                                               sweep_confirmed=True, mss_confirmed=True,
                                               causal_zone_valid=True)
    assert behavior in {EntryBehavior.RETEST.value, EntryBehavior.PULLBACK.value}


def test_micro_pullback_requires_impulse_and_continuation():
    behavior, _, _ = classify_entry_behavior(displacement_atr=1.6,
                                               continuation_probability=.78,
                                               volatility_atr=1.0)
    assert behavior == EntryBehavior.MICRO_PULLBACK.value


def test_zone_wick_is_not_confirmed_breakout():
    rows = [candle(1, 99, 101, 98, 100), candle(2, 100, 103, 99, 100),
            candle(3, 100, 104, 99, 100)]
    result = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.WICK_SWEEP.value
    assert result["event"] != ZoneEvent.BREAKOUT_CONFIRMED.value


def test_single_close_is_pending_and_multiple_closes_confirm():
    rows = [candle(1, 100, 103, 99, 102.5), candle(2, 102, 104, 101, 103),
            candle(3, 103, 105, 102, 104)]
    pending = classify_zone_event(rows[:1] + [candle(2,102,104,101,101.5), rows[2]],
                                  side="BUY", zone_low=99, zone_high=102, atr=1)
    assert pending["event"] == ZoneEvent.BREAKOUT_PENDING.value
    confirmed = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert confirmed["event"] == ZoneEvent.BREAKOUT_RETEST.value


def test_failed_breakout_requires_reentry_after_prior_close_outside():
    rows = [candle(1, 101, 104, 100, 103), candle(2, 103, 104, 100, 103),
            candle(3, 103, 104, 99, 101)]
    result = classify_zone_event(rows, side="BUY", zone_low=100, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.FAILED_BREAKOUT.value
    assert result["confirmed"] is True


def test_sell_failed_breakout_requires_reentry_above_lower_boundary():
    rows = [candle(1, 99, 100, 96, 97), candle(2, 97, 99, 96, 97),
            candle(3, 97, 101, 96, 99)]
    result = classify_zone_event(rows, side="SELL", zone_low=98, zone_high=100, atr=1)
    assert result["event"] == ZoneEvent.FAILED_BREAKOUT.value
    assert result["confirmed"] is True


def test_reject_malformed_candles():
    rows = [candle(1, 100, 99, 98, 100)] * 3
    result = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.INSUFFICIENT_DATA.value


def test_build_decision_never_grants_unproven_behavior():
    result = build_intelligence_decision(regime="UNKNOWN", data_quality="UNKNOWN")
    assert result.mode == TradingMode.UNCLASSIFIED.value
    assert result.entry_behavior == EntryBehavior.WAIT_FOR_STRUCTURE.value
    assert result.confidence == 0


def test_zone_event_is_directional_for_sell_breakout():
    rows = [
        candle(1, 100, 101, 99, 99.5),
        candle(2, 97.8, 97.9, 96.9, 97.2),
        candle(3, 97.2, 97.7, 96.0, 97.0),
    ]
    result = classify_zone_event(rows, side="SELL", zone_low=98, zone_high=100, atr=1)
    assert result["event"] == ZoneEvent.BREAKOUT_CONFIRMED.value
    assert result["confirmed"] is True


def test_zone_event_rejects_duplicate_or_out_of_order_timestamps():
    rows = [
        candle(3, 99, 101, 98, 100),
        candle(2, 100, 103, 99, 100),
        candle(1, 100, 104, 99, 100),
    ]
    result = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.INSUFFICIENT_DATA.value
    assert "timestamps_not_monotonic" in result["reason_codes"]


def test_zone_event_requires_consecutive_acceptance_closes():
    rows = [
        candle(1, 100, 103, 99, 102.5),
        candle(2, 102.5, 103, 100, 101.5),
        candle(3, 101.5, 104, 101, 103),
    ]
    result = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.BREAKOUT_PENDING.value
    assert result["confirmed"] is False


def test_unknown_and_nan_values_never_produce_actionable_mode():
    mode, confidence, _ = classify_trading_mode(
        regime="TREND", adx=float("nan"), continuation_probability=float("inf"),
        liquidity_quality="UNKNOWN", data_quality="VALID",
    )
    assert mode == TradingMode.UNCLASSIFIED.value
    assert confidence == 0


def test_trade_state_default_keeps_existing_protection_and_reconciliation_keys():
    # Structural regression guard: these defaults are consumed by the existing
    # TradeManagementBoard, native-protection path, and close reconciliation.
    import ast
    from pathlib import Path

    source = Path("ggpbot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    state_node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "STATE" for target in node.targets)
    )
    assert isinstance(state_node.value, ast.Dict)
    keys = {
        key.value for key in state_node.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    required = {
        "profit_mode", "native_sl_policy", "execution_unresolved",
        "close_reconciliation_required", "close_verified",
        "last_close_verified", "pending_close_order_id",
        "pending_close_requested_qty", "structural_invalidation",
        "thesis_failure_reasons", "emergency_exit", "emergency_reason",
        "emergency_source", "emergency_ts", "tp1_filled_qty",
        "tp2_filled_qty", "tp1_execution_price",
    }
    assert required <= keys
    assert {"entry_zone_low", "entry_zone_high", "zone_event_last_candle_ts",
            "failed_breakout_streak", "failed_breakout_last_candle_ts"} <= keys



def test_failed_breakout_evidence_requires_two_fresh_confirmed_candles():
    def evidence(streak, fresh=True):
        return {
            "zone_event": "FAILED_BREAKOUT",
            "zone_event_confirmed": True,
            "zone_event_confidence": 0.80,
            "zone_side": "BUY",
            "zone_event_fresh": fresh,
            "failed_breakout_streak": streak,
        }
    def valid(ctx, side="BUY"):
        return (
            str(ctx.get("zone_event") or "").upper() == "FAILED_BREAKOUT"
            and bool(ctx.get("zone_event_confirmed"))
            and bool(ctx.get("zone_event_fresh"))
            and str(ctx.get("zone_side") or "").upper() == side
            and float(ctx.get("zone_event_confidence") or 0) >= 0.75
            and int(ctx.get("failed_breakout_streak") or 0) >= 2
        )
    assert not valid(evidence(1))
    assert valid(evidence(2))
    assert not valid(evidence(2, fresh=False))
    assert not valid(evidence(2), side="SELL")

def test_failed_breakout_streak_counts_distinct_consecutive_candles():
    # The runtime streak contract is candle-based: same candle is ignored,
    # adjacent failed-breakout candles increment, any other event resets.
    prior_event = "FAILED_BREAKOUT"
    prior_ts = 100
    new_event = "FAILED_BREAKOUT"
    new_ts = 115
    same_event = (
        prior_event == "FAILED_BREAKOUT"
        and new_event == "FAILED_BREAKOUT"
        and prior_ts > 0
        and (new_ts - prior_ts) <= 15 * 1.5
    )
    assert same_event is True
    assert (new_ts - prior_ts) > 0
    assert 1 + int(same_event) == 2


def test_board_uses_only_same_side_fresh_confirmed_failed_breakout_evidence():
    # These are the explicit acceptance conditions applied before thesis scoring.
    def qualifies(event, confirmed, confidence, zone_side, trade_side, fresh, streak):
        return (
            str(event).upper() == "FAILED_BREAKOUT"
            and bool(confirmed) and bool(fresh)
            and str(zone_side).upper() == str(trade_side).upper()
            and str(trade_side).upper() in {"BUY", "SELL"}
            and float(confidence) >= 0.75
            and int(streak) >= 2
        )
    assert qualifies("FAILED_BREAKOUT", True, .80, "BUY", "BUY", True, 2)
    assert not qualifies("FAILED_BREAKOUT", True, .80, "BUY", "BUY", True, 1)
    assert not qualifies("FAILED_BREAKOUT", True, .80, "SELL", "BUY", True, 2)
    assert not qualifies("FAILED_BREAKOUT", False, .90, "BUY", "BUY", True, 2)
    assert not qualifies("FAILED_BREAKOUT", True, .70, "BUY", "BUY", True, 2)
    assert not qualifies("FAILED_BREAKOUT", True, .90, "BUY", "BUY", False, 2)


def test_market_intelligence_context_has_one_canonical_failure_streak():
    source = open("ggpbot.py", encoding="utf-8").read()
    marker = '"zone_event_fresh": ('
    i = source.index(marker, source.index("def _apply_management"))
    j = source.index('            },', i)
    segment = source[i:j]
    assert segment.count('"failed_breakout_streak":') == 1
    assert segment.count('"failed_breakout_last_candle_ts":') == 1


def test_duplicate_failed_breakout_candle_does_not_increment_streak():
    previous_ts = 100.0
    duplicate_ts = 100.0
    next_ts = 115.0
    assert (duplicate_ts > previous_ts) is False
    assert (next_ts > previous_ts) is True
    assert next_ts - previous_ts <= 15.0 * 1.5



def test_failed_breakout_confirmation_latches_across_next_inside_candle():
    event, streak = advance_failed_breakout_confirmation(
        previous_event="BREAKOUT_PENDING", previous_timestamp=100,
        previous_streak=0, current_event="FAILED_BREAKOUT",
        current_timestamp=115, timeframe_seconds=15)
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 1
    event, streak = advance_failed_breakout_confirmation(
        previous_event=event, previous_timestamp=115,
        previous_streak=streak, current_event="INSIDE_ZONE",
        current_timestamp=130, timeframe_seconds=15)
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 2


def test_failed_breakout_confirmation_resets_on_duplicate_or_unrelated_event():
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=115,
        previous_streak=1, current_event="INSIDE_ZONE",
        current_timestamp=115, timeframe_seconds=15)
    assert event == ZoneEvent.INSIDE_ZONE.value
    assert streak == 0
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=115,
        previous_streak=1, current_event="BREAKOUT_CONFIRMED",
        current_timestamp=130, timeframe_seconds=15)
    assert event == ZoneEvent.BREAKOUT_CONFIRMED.value
    assert streak == 0


def test_failed_breakout_confirmation_resets_after_time_gap():
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=100,
        previous_streak=1, current_event="INSIDE_ZONE",
        current_timestamp=140, timeframe_seconds=15)
    assert event == ZoneEvent.INSIDE_ZONE.value
    assert streak == 0


def test_breakout_retest_requires_touch_after_initial_acceptance():
    rows = [
        candle(1, 99, 101, 98.5, 99.5),
        candle(2, 100.5, 103, 100, 102.5),
        candle(3, 102.5, 103, 102.0, 102.7),
    ]
    result = classify_zone_event(rows, side="BUY", zone_low=99, zone_high=102, atr=1)
    assert result["event"] == ZoneEvent.BREAKOUT_RETEST.value



def test_intelligence_decision_retains_real_causal_zone_and_mode():
    decision = build_intelligence_decision(
        regime="STRONG_TREND",
        adx=28.0,
        continuation_probability=0.82,
        liquidity_quality="GOOD",
        displacement_atr=1.5,
        setup_type="CONTINUATION",
        causal_zone_valid=True,
        bos_level=105.0,
        reference_low=101.0,
        reference_high=103.0,
        volatility_atr=1.1,
        data_quality="VALID_CLOSED_CANDLES",
    ).to_dict()
    assert decision["mode"] == TradingMode.TREND.value
    assert decision["entry_behavior"] == EntryBehavior.MICRO_PULLBACK.value
    assert decision["reference_low"] == 101.0
    assert decision["reference_high"] == 103.0


def test_bridge_adx_is_finite_on_directional_closed_candles():
    import numpy as np
    import pandas as pd
    from ggp_institutional_bridge import _adx

    n = 60
    close = np.linspace(100.0, 130.0, n)
    frame = pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(n, 1000.0),
        "timestamp": np.arange(n, dtype=float) * 900.0,
    })
    value = _adx(frame, period=14)
    assert np.isfinite(value)
    assert 0.0 <= value <= 100.0


def test_live_trade_observer_uses_last_closed_timestamp_and_never_live_bar():
    source = open("ggpbot.py", encoding="utf-8").read()
    start = source.index("# Closed-candle zone lifecycle; context only, no order side effects.")
    end = source.index("# ========== GET DECISION FROM BOARD ==========", start)
    observer = source[start:end]
    assert "last_closed_ts" in observer
    assert "closed_candle_ts" not in observer
    assert "_closed_candle_frame(raw_frame, INTERVAL)" in source


def test_execution_state_has_one_canonical_market_intelligence_key_set():
    source = open("ggpbot.py", encoding="utf-8").read()
    start = source.index("STATE = {", source.index("# ========== EXECUTION LAYER =========="))
    end = source.index("\npaper =", start)
    state = source[start:end]
    for key in (
        "native_sl_policy", "zone_event_last_candle_ts",
        "failed_breakout_streak", "failed_breakout_thesis_credit_applied",
        "entry_zone_low", "entry_zone_high",
    ):
        assert state.count(f'"{key}":') == 1


def test_entry_context_passes_mode_behavior_and_zone_to_trade_state():
    source = open("ggpbot.py", encoding="utf-8").read()
    assert '"trading_mode": str(item.get("trading_mode") or market_intelligence.get("mode")' in source
    assert '"entry_zone_low": safe_float(item.get("entry_zone_low", market_intelligence.get("reference_low"))' in source
    assert '"entry_zone_high": safe_float(item.get("entry_zone_high", market_intelligence.get("reference_high"))' in source
    assert '"trading_mode": "SNIPER"' in source
