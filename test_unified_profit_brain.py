"""Unit and integration tests for the Unified Profit Management Brain.

These tests exercise pure policy decisions and the TradeManagementBoard adapter.
They do not submit orders, do not touch the exchange, and do not modify the real
trade journal.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import ggpbot
from ggpbot import TradeDecision, TradeDecisionType, TradeManagementBoard
from ggp_profit_brain import (
    BrainAction,
    ProfitBrainDecision,
    TradeMode,
    build_trade_thesis_snapshot,
    classify_trade_mode,
    compare_profit_brain_decisions,
    evaluate_profit_brain,
)

ROOT = Path(__file__).resolve().parent

_BRAIN_STATE_KEYS = (
    "profit_brain_mode",
    "profit_brain_mode_candidate",
    "profit_brain_mode_candidate_count",
    "profit_brain_mode_last_candle_ts",
    "profit_brain_mode_reason",
    "profit_brain_mode_evidence",
    "profit_brain_mode_changed_ts",
    "profit_brain_thesis",
    "profit_brain_trade_id",
    "last_management_closed_candle_ts",
    "exit_risk_snapshot",
)


@pytest.fixture(autouse=True)
def _brain_test_isolation(monkeypatch):
    for key in _BRAIN_STATE_KEYS:
        ggpbot.STATE.pop(key, None)
    ggpbot.STATE.update({
        "open": True,
        "current_symbol": "BTC/USDT:USDT",
        "side": "BUY",
        "entry": 100.0,
        "atr": 1.0,
        "entry_time": time.time(),
        "trade_id": "brain-test-trade",
        "last_management_closed_candle_ts": 1000.0,
        "last_market_data_freshness": "FRESH",
    })
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", False)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_MODE_HYSTERESIS_CANDLES", 2)
    yield
    for key in _BRAIN_STATE_KEYS:
        ggpbot.STATE.pop(key, None)


@contextmanager
def _trade_scope(trade_id):
    """Give one scenario a completely independent trade identity and Brain state."""
    saved = {key: ggpbot.STATE.get(key) for key in _BRAIN_STATE_KEYS}
    saved_trade_id = ggpbot.STATE.get("trade_id")
    saved_symbol = ggpbot.STATE.get("current_symbol")
    for key in _BRAIN_STATE_KEYS:
        ggpbot.STATE.pop(key, None)
    ggpbot.STATE["trade_id"] = trade_id
    ggpbot.STATE["current_symbol"] = "BTC/USDT:USDT"
    ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
    try:
        yield
    finally:
        for key in _BRAIN_STATE_KEYS:
            if saved.get(key) is None:
                ggpbot.STATE.pop(key, None)
            else:
                ggpbot.STATE[key] = saved[key]
        ggpbot.STATE["trade_id"] = saved_trade_id
        ggpbot.STATE["current_symbol"] = saved_symbol


def _context(**overrides):
    ctx = {
        "symbol": "BTC/USDT:USDT",
        "side": "BUY",
        "entry": 100.0,
        "current_price": 100.2,
        "atr": 1.0,
        "roe": 4.0,
        "remaining_qty": 1.0,
        "initial_qty": 1.0,
        "adx": 20.0,
        "adx_slope": 0.0,
        "di_plus": 22.0,
        "di_minus": 20.0,
        "continuation_probability": 0.5,
        "regime": "RANGE",
        "trade_state": "RANGE_CHOP",
        "volume_ratio": 1.0,
        "momentum_strength": 50.0,
        "structure_aligned": False,
        "structure_broken": False,
        "structural_invalidation": False,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp1_price": 0.0,
        "tp2_price": 0.0,
        "synthetic_sl": 99.0,
        "trail_stop": 0.0,
        "trail_active": False,
        "max_price": 100.2,
        "min_price": 99.0,
        "profit_mode": "TWO_STAGE_50_50",
        "indicator_decisions_allowed": True,
        "data_freshness": "FRESH",
        "smart_money": {},
        "momentum": {},
        "market_intelligence": {},
        "liquidity": {},
        "entry_atr": 1.0,
        "entry_time": time.time(),
        "entry_reasons": ["test_entry"],
        "entry_mode": "TREND",
        "entry_behavior": "PULLBACK",
        "entry_mode_confidence": 0.7,
        "entry_order_block": {"low": 99.5, "high": 100.5},
        "entry_fvg": {"lower": 99.8, "upper": 100.2},
        "entry_liquidity": {"event": "SWEEP", "direction": "BULLISH"},
        "entry_golden_zone": {"state": "GOLDEN_CONFLUENCE"},
        "entry_zone_low": 99.5,
        "entry_zone_high": 100.5,
        "entry_spread_pct": 0.03,
        "liquidity_quality": "GOOD",
        "orderbook_depth_usdt": 250000.0,
        "entry_gate_snapshot": {
            "signal_candle_id": "BUY:EMA9_EMA15:1000:PULLBACK:1100:BREAK:1200",
            "ema9": 100.1,
            "ema15": 99.8,
            "vwap": 99.7,
            "cross_direction": "BULLISH",
            "pullback_timestamp": 1100,
            "signal_candle_timestamp": 1200,
            "candle_timeframe": "15m",
        },
        "vwap_cross_snapshot": {
            "ema9": 100.1,
            "vwap": 99.8,
            "state": "ALIGNED_NOT_CROSSING",
            "cross_any": None,
            "signal": "BULLISH",
            "cross_ema9": None,
        },
        "fresh_ema9_vwap_cross": False,
        "fresh_ema50_vwap_cross": False,
        "vwap_cross_direction": "",
        "vwap_cross_trigger": "NONE",
        "vwap_cross_age_seconds": None,
        "vwap_cross_max_age_seconds": 1800,
        "thesis": {
            "thesis_id": "thesis-1",
            "entry_reason": ["test_entry"],
            "market_context": {"regime": "STRONG_TREND"},
        },
    }
    ctx.update(overrides)
    return ctx


def _weak_scalp_context(**overrides):
    ctx = _context(
        adx=14.0, adx_slope=-1.0, continuation_probability=0.35,
        regime="CHOPPY", trade_state="RANGE_CHOP", volume_ratio=0.60,
        momentum_strength=20.0, di_plus=18.0, di_minus=17.0,
        profit_mode="RANGE_SCALP", range_target_price=100.5,
        current_price=100.2, tp1_price=0.0, tp2_price=0.0,
    )
    ctx.update(overrides)
    return ctx


def _momentum_context(**overrides):
    ctx = _context(
        adx=24.0, adx_slope=1.0, continuation_probability=0.60,
        regime="TREND_CONTINUATION", trade_state="HEALTHY_PULLBACK",
        volume_ratio=1.0, momentum_strength=60.0,
        di_plus=28.0, di_minus=18.0, profit_mode="TWO_STAGE_50_50",
        current_price=100.4, tp1_price=0.0, tp2_price=0.0,
    )
    ctx.update(overrides)
    return ctx


def _extended_context(**overrides):
    ctx = _context(
        adx=30.0, adx_slope=1.0, continuation_probability=0.80,
        regime="STRONG_TREND", trade_state="TREND_RIDE",
        volume_ratio=1.30, momentum_strength=70.0,
        di_plus=32.0, di_minus=12.0, profit_mode="TREND_RIDE",
        current_price=100.4, tp1_price=0.0, tp2_price=0.0,
    )
    ctx.update(overrides)
    return ctx


def _trend_riding_context(**overrides):
    ctx = _context(
        adx=24.0, adx_slope=1.0, continuation_probability=0.68,
        regime="TREND_CONTINUATION", trade_state="HEALTHY_PULLBACK",
        volume_ratio=0.90, momentum_strength=65.0,
        di_plus=30.0, di_minus=18.0, profit_mode="TREND_RIDE",
        current_price=100.4, tp1_price=0.0, tp2_price=0.0,
    )
    ctx.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# Mode classification
# ---------------------------------------------------------------------------

def test_weak_market_classifies_scalp():
    mode, reason, evidence, danger = classify_trade_mode(_weak_scalp_context())
    assert mode == TradeMode.SCALP_MODE
    assert danger is False
    assert "range_or_chop_regime" in evidence


def test_momentum_context_classifies_momentum():
    mode, reason, evidence, danger = classify_trade_mode(_momentum_context())
    assert mode == TradeMode.MOMENTUM_MODE
    assert danger is False


def test_strong_expansion_classifies_extended():
    mode, reason, evidence, danger = classify_trade_mode(_extended_context())
    assert mode == TradeMode.EXTENDED_MODE
    assert danger is False


def test_confirmed_trend_without_expansion_classifies_trend_riding():
    mode, reason, evidence, danger = classify_trade_mode(_trend_riding_context())
    assert mode == TradeMode.TREND_RIDING_MODE
    assert danger is False


@pytest.mark.parametrize("danger_override", [
    {"structural_invalidation": True},
    {"structure_broken": True},
    {"trade_state": "PANIC_EXIT"},
    {"trade_state": "MOMENTUM_COLLAPSE"},
    {"thesis_failure_score": 75.0},
])
def test_confirmed_danger_classifies_defensive_immediately(danger_override):
    ctx = _trend_riding_context(**danger_override)
    mode, reason, evidence, danger = classify_trade_mode(ctx)
    assert mode == TradeMode.DEFENSIVE_MODE
    assert danger is True
    assert "confirmed_danger" in evidence


# ---------------------------------------------------------------------------
# Mode transitions and hysteresis
# ---------------------------------------------------------------------------

def test_scalp_can_become_trend_riding_after_two_closed_candles(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
    first = board.evaluate(_weak_scalp_context())
    assert first.mode == TradeMode.SCALP_MODE.value
    ggpbot.STATE["last_management_closed_candle_ts"] = 1001.0
    second = board.evaluate(_trend_riding_context())
    assert second.mode == TradeMode.SCALP_MODE.value
    assert ggpbot.STATE["profit_brain_mode_candidate"] == TradeMode.TREND_RIDING_MODE.value
    ggpbot.STATE["last_management_closed_candle_ts"] = 1002.0
    third = board.evaluate(_trend_riding_context())
    assert third.mode == TradeMode.TREND_RIDING_MODE.value


def test_new_trade_resets_previous_brain_mode(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    ggpbot.STATE["trade_id"] = "trade-a"
    ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
    board.evaluate(_trend_riding_context())
    assert ggpbot.STATE["profit_brain_mode"] == TradeMode.TREND_RIDING_MODE.value
    ggpbot.STATE["trade_id"] = "trade-b"
    ggpbot.STATE["last_management_closed_candle_ts"] = 2000.0
    decision = board.evaluate(_weak_scalp_context())
    assert decision.mode == TradeMode.SCALP_MODE.value
    assert ggpbot.STATE["profit_brain_mode"] == TradeMode.SCALP_MODE.value


def test_confirmed_danger_moves_to_defensive_without_hysteresis(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
    board.evaluate(_trend_riding_context())
    assert ggpbot.STATE["profit_brain_mode"] == TradeMode.TREND_RIDING_MODE.value
    ggpbot.STATE["last_management_closed_candle_ts"] = 1001.0
    danger = _trend_riding_context(structural_invalidation=True)
    decision = board.evaluate(danger)
    assert decision.mode == TradeMode.DEFENSIVE_MODE.value
    assert decision.action == TradeDecisionType.THESIS_FAILURE_EXIT
    assert ggpbot.STATE["profit_brain_mode"] == TradeMode.DEFENSIVE_MODE.value


def test_trend_riding_can_return_to_defensive_on_confirmed_break():
    decision = evaluate_profit_brain(_trend_riding_context(structure_broken=True))
    assert decision.mode == TradeMode.DEFENSIVE_MODE.value
    assert decision.action in {BrainAction.THESIS_FAILURE_EXIT.value, BrainAction.FULL_CLOSE.value}


# ---------------------------------------------------------------------------
# Protection and exit behavior
# ---------------------------------------------------------------------------

def test_normal_pullback_does_not_close_trade():
    ctx = _trend_riding_context(
        trade_state="HEALTHY_PULLBACK", continuation_probability=0.64,
        tp1_hit=True, be_done=True, trail_active=False, trail_stop=0.0,
        current_price=100.5, max_price=101.0, min_price=99.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
    }


def test_confirmed_failed_breakout_can_exit():
    ctx = _trend_riding_context(
        thesis_failure_score=55.0, roe=8.0,
        market_intelligence={
            "zone_event": "FAILED_BREAKOUT", "zone_event_confirmed": True,
            "zone_event_confidence": 0.90, "failed_breakout_streak": 2,
            "zone_event_fresh": True, "zone_side": "BUY",
            "failed_breakout_thesis_credit_applied": False,
        },
        tp1_hit=True, be_done=True, current_price=100.2,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.THESIS_FAILURE_EXIT.value
    assert decision.mode == TradeMode.DEFENSIVE_MODE.value


def test_unconfirmed_failed_breakout_does_not_force_exit():
    ctx = _trend_riding_context(
        thesis_failure_score=55.0, roe=8.0,
        market_intelligence={
            "zone_event": "FAILED_BREAKOUT", "zone_event_confirmed": True,
            "zone_event_confidence": 0.90, "failed_breakout_streak": 1,
            "zone_event_fresh": True, "zone_side": "BUY",
            "failed_breakout_thesis_credit_applied": False,
        },
        tp1_hit=True, be_done=True, current_price=100.2,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action != BrainAction.THESIS_FAILURE_EXIT.value


def test_liquidity_sweep_alone_does_not_close():
    ctx = _trend_riding_context(
        liquidity={"event": "SWEEP", "direction": "BEARISH"},
        tp1_hit=True, be_done=True, current_price=100.5, max_price=101.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
    }


@pytest.mark.parametrize("overrides", [
    {"adx": 12.0, "adx_slope": -1.0},
    {"momentum": {"rsi_value": 82.0, "climax_risk": 0.0}},
    {"thesis_failure_score": 20.0, "thesis_failure_reasons": ["strong_bearish_candle"]},
])
def test_single_indicator_cannot_close_trade(overrides):
    ctx = _trend_riding_context(
        tp1_hit=True, be_done=True, current_price=100.5, max_price=101.0,
    )
    ctx.update(overrides)
    decision = evaluate_profit_brain(ctx)
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
    }


def test_missing_data_is_marked_and_does_not_fabricate_indicator_exit():
    ctx = _context(
        indicator_decisions_allowed=False, data_freshness="STALE",
        structural_invalidation=False, tp1_hit=True, be_done=True,
        current_price=100.2, tp1_price=0.0, tp2_price=0.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.data_quality == "INVALID"
    assert any(item.startswith("market_data:") for item in decision.missing_evidence)
    assert decision.action == BrainAction.HOLD.value
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
        BrainAction.BREAKEVEN.value, BrainAction.TRAIL.value,
        BrainAction.LOCK_PROFITS.value,
    }


def test_price_only_tp1_still_works_during_data_outage():
    ctx = _context(
        indicator_decisions_allowed=False, data_freshness="STALE",
        tp1_price=100.2, current_price=100.3,
        remaining_qty=1.0, initial_qty=1.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.data_quality == "INVALID"
    assert decision.action == BrainAction.PARTIAL_CLOSE.value
    assert decision.params["close_reason"] == "TP1"


def test_tp1_is_exactly_fifty_percent_of_original_filled_quantity():
    ctx = _context(tp1_price=100.2, current_price=100.3, remaining_qty=1.0, initial_qty=1.0)
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.PARTIAL_CLOSE.value
    assert decision.params["ratio"] == pytest.approx(0.5)
    assert decision.params["target_remaining_qty"] == pytest.approx(0.5)
    assert decision.params["close_reason"] == "TP1"


def test_tp2_closes_the_remaining_runner():
    ctx = _context(
        tp1_hit=True, be_done=True, tp2_price=101.0, current_price=101.1,
        remaining_qty=0.5, initial_qty=1.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.FULL_CLOSE.value
    assert decision.params["close_reason"] == "TP2"


def test_range_scalp_single_target_full_close_is_preserved():
    ctx = _weak_scalp_context(range_target_price=100.5, current_price=100.6)
    decision = evaluate_profit_brain(ctx)
    assert decision.execution_plan == "RANGE_SCALP"
    assert decision.action == BrainAction.FULL_CLOSE.value
    assert decision.params["close_reason"] == "SCALP_TARGET"


def test_range_scalp_holds_before_target():
    ctx = _weak_scalp_context(range_target_price=100.5, current_price=100.2)
    decision = evaluate_profit_brain(ctx)
    assert decision.execution_plan == "RANGE_SCALP"
    assert decision.action == BrainAction.HOLD.value


def test_execution_unresolved_blocks_new_decisions():
    ctx = _trend_riding_context(execution_unresolved=True, tp1_price=100.2, current_price=100.3)
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.HOLD.value
    assert decision.reason == "exchange_execution_or_recovery_unresolved"


def test_emergency_exit_is_explicit_and_has_no_entry_authorization():
    ctx = _trend_riding_context(emergency_exit=True, emergency_reason="MANUAL_KILL_SWITCH")
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.EMERGENCY_EXIT.value
    assert decision.entry_authorized is False
    assert decision.to_dict()["entry_authorized"] is False


# ---------------------------------------------------------------------------
# Thesis snapshot and data quality
# ---------------------------------------------------------------------------

def test_thesis_snapshot_captures_entry_context_and_marks_missing_fields():
    snapshot = build_trade_thesis_snapshot(_context())
    assert snapshot["signal_candle_id"] == "BUY:EMA9_EMA15:1000:PULLBACK:1100:BREAK:1200"
    assert snapshot["entry_ema9"] == pytest.approx(100.1)
    assert snapshot["entry_extension_atr"] == pytest.approx(0.1)
    assert "unknown_fields" in snapshot
    assert snapshot["entry_atr"] == pytest.approx(1.0)

    missing = build_trade_thesis_snapshot(_context(entry_gate_snapshot={}))
    assert missing["signal_candle_id"] == "UNKNOWN"
    assert "signal_candle_id" in missing["unknown_fields"]


def test_decision_has_unique_id_evidence_and_data_quality():
    first = evaluate_profit_brain(_trend_riding_context())
    second = evaluate_profit_brain(_trend_riding_context())
    assert first.decision_id and second.decision_id
    assert first.decision_id != second.decision_id
    assert first.data_quality == "GOOD"
    assert first.evidence_for
    assert first.entry_authorized is False


def test_missing_required_indicator_is_unknown_data_not_defensive():
    ctx = _trend_riding_context()
    ctx.pop("adx")
    decision = evaluate_profit_brain(ctx)
    assert decision.data_quality == "UNKNOWN_DATA"
    assert decision.mode != TradeMode.DEFENSIVE_MODE.value
    assert decision.confirmed_danger is False
    assert "adx" in decision.missing_evidence


def test_unknown_data_does_not_trail_or_change_protection():
    ctx = _trend_riding_context(
        tp1_hit=True, be_done=False, trail_active=False,
        current_price=100.5, max_price=101.0,
    )
    ctx.pop("adx")
    decision = evaluate_profit_brain(ctx)
    assert decision.data_quality == "UNKNOWN_DATA"
    assert decision.mode != TradeMode.DEFENSIVE_MODE.value
    assert decision.confirmed_danger is False
    assert decision.action == BrainAction.HOLD.value


def test_stale_data_does_not_trail_or_change_protection():
    ctx = _trend_riding_context(
        data_freshness="STALE", tp1_hit=True, be_done=False,
        trail_active=False, current_price=100.5, max_price=101.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.data_quality == "INVALID"
    assert decision.mode != TradeMode.DEFENSIVE_MODE.value
    assert decision.confirmed_danger is False
    assert decision.action == BrainAction.HOLD.value


def test_range_execution_plan_forces_effective_scalp_mode():
    ctx = _extended_context(
        profit_mode="RANGE_SCALP", range_target_price=100.5, current_price=100.4,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.execution_plan == "RANGE_SCALP"
    assert decision.mode == TradeMode.SCALP_MODE.value
    assert decision.mode_candidate in {
        TradeMode.EXTENDED_MODE.value,
        TradeMode.TREND_RIDING_MODE.value,
        TradeMode.MOMENTUM_MODE.value,
    }
    assert "mode_aligned_to_range_execution_plan" in decision.evidence_for


def test_vwap_ema_context_is_captured_and_aligned():
    decision = evaluate_profit_brain(_context())
    context = decision.vwap_ema_context
    assert context["ema9"] == pytest.approx(100.1)
    assert context["ema15"] == pytest.approx(99.8)
    assert context["vwap"] == pytest.approx(99.8)
    assert context["alignment"] == "ALIGNED"
    assert context["source"] == "CLOSED_CANDLE_VWAP_EMA_MANAGEMENT_CONTEXT"
    assert "vwap_ema_aligned" in decision.evidence_for


def test_vwap_ema_opposition_alone_does_not_close():
    ctx = _trend_riding_context(
        vwap_cross_snapshot={
            "ema9": 99.5, "vwap": 100.0, "state": "ALIGNED_NOT_CROSSING",
            "cross_any": "BEARISH", "signal": "BEARISH",
        },
        fresh_ema9_vwap_cross=True,
        vwap_cross_direction="BEARISH",
        vwap_cross_age_seconds=60,
        vwap_cross_max_age_seconds=1800,
        tp1_hit=True, be_done=True, current_price=100.5, max_price=101.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.vwap_ema_context["alignment"] == "OPPOSED"
    assert "vwap_ema_opposed" in decision.evidence_against
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
    }


def test_vwap_ema_no_chase_evidence_does_not_close_alone():
    ctx = _trend_riding_context(
        tp1_hit=True, be_done=True, current_price=103.0, max_price=103.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.vwap_ema_context["no_chase_risk"] is True
    assert any(item.startswith("price_vwap_extension_atr") for item in decision.evidence_against)
    assert decision.action not in {
        BrainAction.FULL_CLOSE.value, BrainAction.THESIS_FAILURE_EXIT.value,
    }


def test_board_brain_decision_exposes_vwap_ema_context(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    decision = board.evaluate(_context())
    assert decision.vwap_ema_context["alignment"] == "ALIGNED"
    assert decision.vwap_ema_context["ema9"] == pytest.approx(100.1)


def test_production_defaults_keep_brain_in_safe_shadow_mode(tmp_path):
    env = os.environ.copy()
    env.update({
        "PAPER_MODE": "true",
        "ENABLE_LIVE_TRADING": "false",
        "INTERVAL": "15m",
        "TRADE_JOURNAL_PATH": str(tmp_path / "journal.sqlite3"),
        "PYTHONPATH": str(ROOT),
    })
    env.pop("ENABLE_UNIFIED_PROFIT_BRAIN", None)
    env.pop("PROFIT_BRAIN_SHADOW_MODE", None)
    script = (
        "import json, ggpbot\n"
        "print(json.dumps({'brain': ggpbot.ENABLE_UNIFIED_PROFIT_BRAIN, "
        "'shadow': ggpbot.PROFIT_BRAIN_SHADOW_MODE}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["brain"] is True
    assert payload["shadow"] is False


def test_live_profit_brain_is_shadow_only_without_explicit_authority():
    assert ggpbot._resolve_profit_brain_shadow_mode(
        paper_mode=False, requested_shadow=False, live_authority=False
    ) is True


def test_live_profit_brain_requires_separate_explicit_authority_opt_in():
    assert ggpbot._resolve_profit_brain_shadow_mode(
        paper_mode=False, requested_shadow=False, live_authority=True
    ) is False


def test_paper_profit_brain_can_be_authoritative_without_shadow():
    assert ggpbot._resolve_profit_brain_shadow_mode(
        paper_mode=True, requested_shadow=False, live_authority=False
    ) is False


def test_isolated_normal_pullback_and_liquidity_sweep_are_not_defensive(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    with _trade_scope("trade-normal-pullback"):
        decision = board.evaluate(_trend_riding_context(
            trade_state="HEALTHY_PULLBACK", tp1_hit=True, be_done=True,
            current_price=100.5, max_price=101.0,
        ))
        assert decision.mode != TradeMode.DEFENSIVE_MODE.value
        assert decision.action not in {
            TradeDecisionType.FULL_CLOSE, TradeDecisionType.THESIS_FAILURE_EXIT,
        }
    with _trade_scope("trade-liquidity-sweep"):
        decision = board.evaluate(_trend_riding_context(
            liquidity={"event": "SWEEP", "direction": "BEARISH"},
            tp1_hit=True, be_done=True, current_price=100.5, max_price=101.0,
        ))
        assert decision.mode != TradeMode.DEFENSIVE_MODE.value
        assert decision.action not in {
            TradeDecisionType.FULL_CLOSE, TradeDecisionType.THESIS_FAILURE_EXIT,
        }


def test_mode_specific_tp2_policy_closes_scalp_but_holds_extended():
    scalp_ctx = _weak_scalp_context(
        tp1_hit=True, be_done=True, tp2_price=103.5, current_price=100.5,
        continuation_probability=0.52, structure_aligned=False,
        profit_mode="TWO_STAGE_50_50",
    )
    scalp = evaluate_profit_brain(scalp_ctx)
    assert scalp.mode == TradeMode.SCALP_MODE.value
    assert scalp.action == BrainAction.FULL_CLOSE.value
    assert scalp.params["close_reason"] == "TP2_ADAPTIVE_STRICT_EXIT"

    extended_ctx = _extended_context(
        tp1_hit=True, be_done=True, tp2_price=103.5, current_price=100.5,
        continuation_probability=0.68, structure_aligned=True,
    )
    extended = evaluate_profit_brain(extended_ctx)
    assert extended.mode == TradeMode.EXTENDED_MODE.value
    assert extended.action != BrainAction.FULL_CLOSE.value

    trend_ctx = _trend_riding_context(
        tp1_hit=True, be_done=True, tp2_price=103.5, current_price=100.5,
        continuation_probability=0.68, structure_aligned=True,
    )
    trend = evaluate_profit_brain(trend_ctx)
    assert trend.mode == TradeMode.TREND_RIDING_MODE.value
    assert trend.action != BrainAction.FULL_CLOSE.value

    momentum_ctx = _momentum_context(
        tp1_hit=True, be_done=True, tp2_price=103.5, current_price=100.5,
        continuation_probability=0.60, structure_aligned=True,
    )
    momentum = evaluate_profit_brain(momentum_ctx)
    assert momentum.mode == TradeMode.MOMENTUM_MODE.value
    assert momentum.action != BrainAction.FULL_CLOSE.value


def test_single_trade_scalp_to_trend_riding_to_defensive(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    with _trade_scope("transition-trade"):
        scalp_ctx = _weak_scalp_context(
            tp1_hit=True, be_done=True, tp2_price=102.5, current_price=100.5,
            continuation_probability=0.52, structure_aligned=True,
            profit_mode="TWO_STAGE_50_50", max_price=100.5,
        )
        ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
        first = board.evaluate(scalp_ctx)
        assert first.mode == TradeMode.SCALP_MODE.value
        assert first.action != TradeDecisionType.FULL_CLOSE

        trend_ctx = _trend_riding_context(
            tp1_hit=True, be_done=True, tp2_price=102.5, current_price=100.5,
            continuation_probability=0.68, structure_aligned=True, max_price=100.5,
        )
        ggpbot.STATE["last_management_closed_candle_ts"] = 1001.0
        second = board.evaluate(trend_ctx)
        assert second.mode == TradeMode.SCALP_MODE.value
        assert ggpbot.STATE["profit_brain_mode_candidate"] == TradeMode.TREND_RIDING_MODE.value
        ggpbot.STATE["last_management_closed_candle_ts"] = 1002.0
        third = board.evaluate(trend_ctx)
        assert third.mode == TradeMode.TREND_RIDING_MODE.value
        assert third.action != TradeDecisionType.FULL_CLOSE

        danger_ctx = _trend_riding_context(
            tp1_hit=True, be_done=True, tp2_price=102.5, current_price=100.5,
            continuation_probability=0.68, structure_aligned=True,
            structural_invalidation=True,
        )
        ggpbot.STATE["last_management_closed_candle_ts"] = 1003.0
        fourth = board.evaluate(danger_ctx)
        assert fourth.mode == TradeMode.DEFENSIVE_MODE.value
        assert fourth.action == TradeDecisionType.THESIS_FAILURE_EXIT


def test_mode_trail_factors_not_adopted_without_approval():
    from ggp_profit_brain import _mode_trail_factor, trailing_comparison

    for mode in TradeMode:
        assert _mode_trail_factor(mode.value) == 1.0

    ctx = _trend_riding_context(
        tp1_hit=True, be_done=True, trail_active=False, trail_stop=0.0,
        current_price=102.0, max_price=103.0, atr=1.0,
        continuation_probability=0.60, adx=25.0,
    )
    decision = evaluate_profit_brain(ctx)
    assert decision.action == BrainAction.TRAIL.value
    assert decision.params["multiplier"] == pytest.approx(
        trailing_comparison(ctx, 1.0)["multiplier"]
    )

    comparison_085 = trailing_comparison(ctx, 0.85)
    comparison_100 = trailing_comparison(ctx, 1.0)
    comparison_115 = trailing_comparison(ctx, 1.15)
    assert comparison_085["stop_distance_atr"] < comparison_100["stop_distance_atr"] < comparison_115["stop_distance_atr"]
    assert comparison_085["locked_profit_price"] > comparison_100["locked_profit_price"] > comparison_115["locked_profit_price"]
    assert comparison_085["room_for_pullback_atr"] < comparison_100["room_for_pullback_atr"] < comparison_115["room_for_pullback_atr"]
    assert all(item["be_floor_respected"] for item in (comparison_085, comparison_100, comparison_115))
    assert comparison_085["early_exit_proxy"]["1.5"] is True
    assert comparison_115["early_exit_proxy"]["1.5"] is False


def test_shadow_comparison_flags_legacy_protection_change_on_invalid_data():
    legacy = TradeDecision(TradeDecisionType.BREAKEVEN, params={"new_sl": 100.1}, reason="legacy")
    brain = ProfitBrainDecision(
        decision_id="brain-invalid", created_at=time.time(), symbol="BTC/USDT:USDT",
        side="BUY", mode=TradeMode.MOMENTUM_MODE.value,
        mode_candidate=TradeMode.MOMENTUM_MODE.value,
        execution_plan="TWO_STAGE_50_50", action=BrainAction.HOLD.value,
        reason="data_insufficient_mode_unchanged", data_quality="UNKNOWN_DATA",
    )
    comparison = compare_profit_brain_decisions(legacy, brain, _context())
    assert "legacy_protection_change_on_invalid_data" in comparison["risk_flags"]
    assert "brain_protection_change_on_invalid_data" not in comparison["risk_flags"]


def test_close_reconciliation_required_blocks_new_decisions():
    decision = evaluate_profit_brain(_context(
        close_reconciliation_required=True, tp1_price=100.2, current_price=100.3,
    ))
    assert decision.action == BrainAction.HOLD.value
    assert decision.reason == "exchange_execution_or_recovery_unresolved"


def test_journal_snapshot_includes_profit_brain_state():
    ggpbot.STATE["profit_brain_mode"] = TradeMode.TREND_RIDING_MODE.value
    ggpbot.STATE["profit_brain_thesis"] = {"thesis_id": "t1", "unknown_fields": []}
    snapshot = ggpbot._journal_snapshot_state()
    assert snapshot["profit_brain_mode"] == TradeMode.TREND_RIDING_MODE.value
    assert snapshot["profit_brain_thesis"]["thesis_id"] == "t1"


def test_restart_preserves_brain_mode_for_same_trade(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    ggpbot.STATE["trade_id"] = "restart-trade"
    ggpbot.STATE["profit_brain_trade_id"] = "restart-trade"
    ggpbot.STATE["profit_brain_mode"] = TradeMode.TREND_RIDING_MODE.value
    ggpbot.STATE["last_management_closed_candle_ts"] = 1000.0
    board = TradeManagementBoard()
    decision = board.evaluate(_trend_riding_context())
    assert decision.mode == TradeMode.TREND_RIDING_MODE.value
    assert ggpbot.STATE["profit_brain_mode"] == TradeMode.TREND_RIDING_MODE.value


def test_shadow_independent_scenarios_do_not_bleed(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", True)
    board = TradeManagementBoard()
    with _trade_scope("shadow-normal"):
        decision = board.evaluate(_trend_riding_context(
            trade_state="HEALTHY_PULLBACK", tp1_hit=True, be_done=True,
            current_price=100.5, max_price=101.0,
        ))
        assert decision.mode == ""  # Shadow returns the legacy decision
        assert ggpbot.STATE["profit_brain_mode"] != TradeMode.DEFENSIVE_MODE.value
    with _trade_scope("shadow-sweep"):
        board.evaluate(_trend_riding_context(
            liquidity={"event": "SWEEP", "direction": "BEARISH"},
            tp1_hit=True, be_done=True, current_price=100.5, max_price=101.0,
        ))
        assert ggpbot.STATE["profit_brain_mode"] != TradeMode.DEFENSIVE_MODE.value
    with _trade_scope("shadow-tp1"):
        decision = board.evaluate(_context(tp1_price=100.2, current_price=100.3))
        assert decision.action == TradeDecisionType.PARTIAL_CLOSE
    with _trade_scope("shadow-hard"):
        decision = board.evaluate(_trend_riding_context(structural_invalidation=True))
        assert decision.action == TradeDecisionType.THESIS_FAILURE_EXIT

    events = [
        event for event in ggpbot.TRADE_MANAGEMENT_LOG.snapshot(limit=200)
        if event.get("event_type") == "PROFIT_BRAIN_SHADOW"
    ]
    assert events
    assert any(
        "legacy_params" in event and "brain_params" in event
        and "brain_mode" in event and "execution_plan" in event
        for event in events
    )


# ---------------------------------------------------------------------------
# Board adapter: legacy OFF, shadow ON, brain authoritative
# ---------------------------------------------------------------------------

def test_feature_flag_off_preserves_legacy_decision(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", False)
    board = TradeManagementBoard()
    decision = board.evaluate(_context(tp1_price=100.2, current_price=100.3))
    assert decision.action == TradeDecisionType.PARTIAL_CLOSE
    assert decision.decision_id == ""
    assert decision.mode == ""


def test_shadow_mode_returns_legacy_and_records_comparison(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", True)
    board = TradeManagementBoard()
    decision = board.evaluate(_context(tp1_price=100.2, current_price=100.3))
    assert decision.action == TradeDecisionType.PARTIAL_CLOSE
    assert decision.decision_id == ""  # legacy decision returned
    events = ggpbot.TRADE_MANAGEMENT_LOG.snapshot(limit=50)
    assert any(event.get("event_type") == "PROFIT_BRAIN_SHADOW" for event in events)


def test_brain_mode_returns_brain_decision_with_identity_and_mode(monkeypatch):
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", False)
    board = TradeManagementBoard()
    decision = board.evaluate(_context(tp1_price=100.2, current_price=100.3))
    assert decision.action == TradeDecisionType.PARTIAL_CLOSE
    assert decision.decision_id
    assert decision.mode in {item.value for item in TradeMode}
    assert decision.data_quality in {"GOOD", "UNKNOWN_DATA", "INVALID"}
    assert decision.evidence_for


@pytest.mark.parametrize("shadow", [True, False])
def test_board_evaluation_is_decision_only_and_cannot_close(monkeypatch, shadow):
    calls = []
    monkeypatch.setattr(ggpbot, "safe_close_position", lambda *a, **k: calls.append("full_close"))
    monkeypatch.setattr(ggpbot, "safe_close_partial", lambda *a, **k: calls.append("partial_close"))
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", True)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", shadow)
    board = TradeManagementBoard()
    monkeypatch.setattr(ggpbot.LiveTradeManager, "_execute_decision",
                        lambda self, *a, **k: calls.append("execute"))
    decision = board.evaluate(_context(tp1_price=100.2, current_price=100.3))
    assert decision.action == TradeDecisionType.PARTIAL_CLOSE
    assert calls == []


def test_shadow_comparison_reports_parameter_differences():
    legacy = TradeDecision(TradeDecisionType.TRAIL, params={"new_stop": 100.0}, reason="legacy")
    brain = ProfitBrainDecision(
        decision_id="brain-1", created_at=time.time(), symbol="BTC/USDT:USDT",
        side="BUY", mode=TradeMode.TREND_RIDING_MODE.value,
        mode_candidate=TradeMode.TREND_RIDING_MODE.value,
        execution_plan="TWO_STAGE_50_50", action=BrainAction.TRAIL.value,
        params={"new_stop": 100.5}, reason="brain",
    )
    comparison = compare_profit_brain_decisions(legacy, brain, _context())
    assert comparison["match"] is False
    assert comparison["differences"]["new_stop"] == {"legacy": 100.0, "brain": 100.5}
    assert comparison["entry_authorized"] is False


def test_board_does_not_execute_and_keeps_single_authority_source_shape():
    source = (__import__("pathlib").Path(__file__).with_name("ggpbot.py")).read_text(encoding="utf-8")
    assert "decision = self.board.evaluate(context)" in source
    assert "safe_close_partial(symbol, ratio" in source
    assert "safe_close_position(symbol, close_reason=close_reason)" in source
    assert "def _evaluate_legacy" in source
    assert "def evaluate(self, context: Dict) -> TradeDecision" in source
    assert "self._publish_brain_decision" in source
    assert "self._record_shadow_comparison" in source
