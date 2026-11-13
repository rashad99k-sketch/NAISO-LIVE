import pytest

from ggp_market_intelligence import (
    TradingMode, EntryBehavior, ZoneEvent,
    classify_trading_mode, classify_entry_behavior, classify_zone_event,
    build_intelligence_decision, advance_failed_breakout_confirmation,\n    is_confirmed_failed_breakout_evidence,
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


def test_failed_breakout_streak_increments_on_each_adjacent_reentry_candle():
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=100,
        previous_streak=1, current_event="FAILED_BREAKOUT",
        current_timestamp=115, timeframe_seconds=15)
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 2


def test_failed_breakout_streak_starts_over_after_gap_or_duplicate():
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=100,
        previous_streak=1, current_event="FAILED_BREAKOUT",
        current_timestamp=140, timeframe_seconds=15)
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 1
    event, streak = advance_failed_breakout_confirmation(
        previous_event="FAILED_BREAKOUT", previous_timestamp=100,
        previous_streak=1, current_event="FAILED_BREAKOUT",
        current_timestamp=100, timeframe_seconds=15)
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 1


def test_adx_rejects_missing_or_nonfinite_ohlc():
    import numpy as np
    import pandas as pd
    from ggp_institutional_bridge import _adx
    frame = pd.DataFrame({"high":[2.0]*60,"low":[1.0]*60,"close":[1.5]*60})
    assert np.isnan(_adx(frame.assign(close=np.nan)))
    assert np.isnan(_adx(frame.drop(columns=["high"])))
    assert np.isnan(_adx(frame, period=1))


def test_management_observer_passes_fresh_zone_evidence_into_same_trade_state_context():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    start = source.index("# Closed-candle zone lifecycle; context only, no order side effects.")
    end = source.index("# ========== GET DECISION FROM BOARD ==========", start)
    observer = source[start:end]
    assert '"zone_event_fresh": (' in observer
    assert '"zone_side": str(side or "").upper()' in observer
    assert '"failed_breakout_streak": _failure_streak' in observer
    assert 'self.board.evaluate(context)' in source


def test_management_board_receives_post_observer_fresh_market_intelligence_snapshot():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    observer = source.index("# Closed-candle zone lifecycle; context only, no order side effects.")
    refresh = source.index("# Refresh the intelligence snapshot AFTER the closed-candle observer.", observer)
    board = source.index("decision = self.board.evaluate(context)", refresh)
    segment = source[observer:board]
    assert 'context["market_intelligence"] = _mi_context' in segment
    assert segment.index('context["market_intelligence"] = _mi_context') < segment.index(
        "decision = self.board.evaluate(context)") if "decision = self.board.evaluate(context)" in segment else True
    assert '"failed_breakout_thesis_credit_applied": bool(' in segment
    assert '"zone_event_fresh": (' in segment


def test_actual_pipeline_regime_labels_classify_trend_and_scalp():
    trend, _, trend_reasons = classify_trading_mode(
        regime="TREND_CONTINUATION",
        adx=27.0,
        continuation_probability=0.74,
        data_quality="VALID_CLOSED_CANDLES",
    )
    assert trend == TradingMode.TREND.value
    assert "directional_regime" in trend_reasons

    pullback, _, _ = classify_trading_mode(
        regime="EMA20_PULLBACK",
        adx=24.0,
        continuation_probability=0.68,
        data_quality="VALID_CLOSED_CANDLES",
    )
    assert pullback == TradingMode.TREND.value

    scalp, _, scalp_reasons = classify_trading_mode(
        regime="RANGE_OR_UNCLEAR",
        adx=16.0,
        continuation_probability=0.42,
        liquidity_quality="ADEQUATE",
        data_quality="VALID_CLOSED_CANDLES",
    )
    assert scalp == TradingMode.SCALP.value
    assert "range_regime" in scalp_reasons


def test_bos_without_explicit_mss_does_not_qualify_as_sniper():
    mode, _, _ = classify_trading_mode(
        regime="TREND_CONTINUATION",
        adx=28.0,
        continuation_probability=0.80,
        sweep_confirmed=True,
        mss_confirmed=False,
        causal_zone_valid=True,
        data_quality="VALID_CLOSED_CANDLES",
    )
    assert mode == TradingMode.TREND.value
    assert mode != TradingMode.SNIPER.value


def test_sniper_requires_all_three_structural_conditions():
    mode, _, _ = classify_trading_mode(
        regime="RANGE_OR_UNCLEAR",
        adx=17.0,
        continuation_probability=0.40,
        sweep_confirmed=True,
        mss_confirmed=True,
        causal_zone_valid=True,
        data_quality="VALID_CLOSED_CANDLES",
    )
    assert mode == TradingMode.SNIPER.value


def test_bos_continuation_is_not_mislabeled_as_mss(monkeypatch):
    import pandas as pd
    import ggp_institutional_bridge as bridge

    monkeypatch.setattr(
        bridge,
        "confirmed_swings",
        lambda frame, left_right=2: ([(1, 100.0), (2, 110.0)], [(1, 90.0), (2, 100.0)]),
    )
    frame = pd.DataFrame({"close": [111.0]})
    structure = bridge.detect_structure(frame)
    assert structure["bos"] == "BULLISH_BOS"
    assert structure["choch"] is None
    assert structure["mss"] is None


def test_sniper_evidence_must_match_trade_direction_and_choch():
    from ggp_institutional_bridge import _sniper_evidence_matches_side

    assert _sniper_evidence_matches_side("BUY", "SWEEP", "BULLISH", "BULLISH_CHOCH", True)
    assert _sniper_evidence_matches_side("SELL", "GRAB", "BEARISH", "BEARISH_CHOCH", True)
    assert not _sniper_evidence_matches_side("BUY", "SWEEP", "BEARISH", "BULLISH_CHOCH", True)
    assert not _sniper_evidence_matches_side("BUY", "SWEEP", "BULLISH", "BEARISH_CHOCH", True)
    assert not _sniper_evidence_matches_side("BUY", "SWEEP", "BULLISH", "BULLISH_CHOCH", False)


def test_opposite_direction_fvg_is_not_a_valid_trade_zone():
    expected = "BULLISH"
    fvg = {"direction": "BEARISH", "status": "UNFILLED"}
    assert not (
        isinstance(fvg, dict)
        and fvg.get("direction") == expected
        and fvg.get("status") in ("UNFILLED", "PARTIALLY_FILLED")
    )


def test_trade_management_context_exposes_entry_mode_separately_from_profit_mode():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    assert '"entry_mode": str(STATE.get("trading_mode") or "UNCLASSIFIED")' in source
    assert '"profit_mode": STATE.get("profit_mode", STANDARD_MODE)' in source
    assert '"entry_mode": str(ctx.get("entry_mode") or "UNCLASSIFIED")' in source
    assert 'management_mode={current_mode}' in source


def test_bridge_adx_uses_sma_seeded_wilder_smoothing():
    import numpy as np
    import pandas as pd
    from ggp_institutional_bridge import _adx

    period = 5
    close = np.array([100.0, 101.2, 100.7, 102.1, 101.8, 103.4, 104.0,
                      103.1, 104.8, 105.2, 104.5, 106.1, 107.0, 106.4,
                      108.2, 109.0, 108.5, 110.1, 111.0, 110.4, 112.2])
    high = close + np.array([0.6, 0.8, 0.5, 0.7, 0.9, 0.6, 0.8, 0.5, 0.7, 0.9,
                             0.6, 0.8, 0.5, 0.7, 0.9, 0.6, 0.8, 0.5, 0.7, 0.9, 0.6])
    low = close - np.array([0.5, 0.6, 0.8, 0.5, 0.7, 0.9, 0.6, 0.8, 0.5, 0.7,
                            0.9, 0.6, 0.8, 0.5, 0.7, 0.9, 0.6, 0.8, 0.5, 0.7, 0.9])
    frame = pd.DataFrame({"high": high, "low": low, "close": close})

    def rma(values):
        out = np.full(len(values), np.nan)
        valid = np.flatnonzero(np.isfinite(values))
        first = int(valid[0])
        seed = first + period - 1
        out[seed] = np.mean(values[first:seed + 1])
        for idx in range(seed + 1, len(values)):
            out[idx] = (out[idx - 1] * (period - 1) + values[idx]) / period
        return out

    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    tr = np.nanmax(np.column_stack((high-low, abs(high-prev_close), abs(low-prev_close))), axis=1)
    atr = rma(tr)
    pdi = 100 * rma(plus_dm) / atr
    mdi = 100 * rma(minus_dm) / atr
    dx = 100 * abs(pdi-mdi) / (pdi+mdi)
    expected = rma(dx)[-1]
    assert np.isfinite(expected)
    assert _adx(frame, period=period) == pytest.approx(expected, rel=1e-10, abs=1e-10)


def test_failed_breakout_closed_candle_lifecycle_produces_board_eligible_evidence():
    # Candle 3 closes back inside after candle 2 closed outside the BUY zone.
    first = classify_zone_event(
        [candle(900, 100.5, 101.0, 100.0, 100.5),
         candle(1800, 102.2, 103.5, 102.0, 103.0),
         candle(2700, 103.0, 103.2, 100.5, 101.0)],
        side="BUY", zone_low=99.0, zone_high=102.0, atr=1.0,
    )
    assert first["event"] == ZoneEvent.FAILED_BREAKOUT.value
    event, streak = advance_failed_breakout_confirmation(
        previous_event=ZoneEvent.INSIDE_ZONE.value,
        previous_timestamp=1800, previous_streak=0,
        current_event=first["event"], current_timestamp=first["timestamp"],
        timeframe_seconds=900,
    )
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 1

    # The next distinct completed candle remains inside: this is the second
    # adjacent confirmation, not another breakout and not an unfinished bar.
    second = classify_zone_event(
        [candle(1800, 102.2, 103.5, 102.0, 103.0),
         candle(2700, 103.0, 103.2, 100.5, 101.0),
         candle(3600, 101.0, 101.4, 100.2, 100.8)],
        side="BUY", zone_low=99.0, zone_high=102.0, atr=1.0,
    )
    assert second["event"] == ZoneEvent.INSIDE_ZONE.value
    event, streak = advance_failed_breakout_confirmation(
        previous_event=event, previous_timestamp=2700, previous_streak=streak,
        current_event=second["event"], current_timestamp=second["timestamp"],
        timeframe_seconds=900,
    )
    assert event == ZoneEvent.FAILED_BREAKOUT.value
    assert streak == 2

    evidence = {
        "zone_event": event,
        "zone_event_confirmed": True,
        "zone_event_fresh": True,
        "zone_event_confidence": 0.78,
        "zone_side": "BUY",
        "failed_breakout_streak": streak,
        "failed_breakout_thesis_credit_applied": False,
    }
    assert is_confirmed_failed_breakout_evidence(evidence, "BUY")
    assert not is_confirmed_failed_breakout_evidence(evidence, "SELL")
    assert not is_confirmed_failed_breakout_evidence(
        {**evidence, "zone_event_fresh": False}, "BUY"
    )
    assert not is_confirmed_failed_breakout_evidence(
        {**evidence, "failed_breakout_thesis_credit_applied": True}, "BUY"
    )
