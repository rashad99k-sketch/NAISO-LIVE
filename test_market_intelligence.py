import pytest

from ggp_market_intelligence import (
    TradingMode, EntryBehavior, ZoneEvent,
    classify_trading_mode, classify_entry_behavior, classify_zone_event,
    build_intelligence_decision,
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
    assert confirmed["event"] == ZoneEvent.BREAKOUT_CONFIRMED.value


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
