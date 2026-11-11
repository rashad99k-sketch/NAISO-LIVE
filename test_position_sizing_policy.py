"""Position sizing regression: 20% of real available balance at 20x.

Proves the capital-allocation policy end to end from the available balance to the
final exchange quantity, including the mandatory DOGE/USDT example, BUY/SELL
symmetry, the independent stop-risk gate, and the fail-closed balance rules.
No network or live keys are used.
"""
import math
import os
from pathlib import Path

import pytest

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("BINGX_API_KEY", "")
os.environ.setdefault("BINGX_API_SECRET", "")

from ggp_position_sizing import (
    POLICY_LEVERAGE,
    STRONG_TRADE_ALLOCATION_PCT,
    TRADE_ALLOCATION_PCT,
    allocation_for_classification,
    calculate_position_size,
    effective_sizing_classification,
)


def _size(balance, entry, stop_pct, side="BUY", classification="TREND",
          contract_size=1.0, risk_pct=1.5, buffer_pct=0.05, leverage=20):
    stop = entry * (1 - stop_pct / 100.0) if side == "BUY" else entry * (1 + stop_pct / 100.0)
    return calculate_position_size(
        classification=classification, available_balance=balance, entry_price=entry,
        stop_price=stop, side=side, leverage=leverage, contract_size=contract_size,
        max_trade_risk_pct=risk_pct, stop_execution_buffer_pct=buffer_pct)


# 1) Available Balance = 100 -> Target Margin = 20
def test_balance_100_target_margin_20():
    r = _size(100.0, 0.06, 0.05, risk_pct=1.0)  # tiny stop so allocation binds
    assert r.allocation_pct == pytest.approx(TRADE_ALLOCATION_PCT)
    assert r.target_margin == pytest.approx(20.0)


# 2) 20x -> Target Notional = 400
def test_leverage_20_target_notional_400():
    r = _size(100.0, 0.06, 0.05, risk_pct=1.0)
    assert POLICY_LEVERAGE == 20
    assert r.target_notional == pytest.approx(400.0)


# 3) DOGE @ 0.06 -> raw quantity 6666.666666 (allocation binding, not risk)
def test_doge_raw_quantity_6666_666666():
    r = _size(100.0, 0.06, 0.05, risk_pct=1.0)
    # raw quantity from the allocation target (before risk gate would reduce it)
    raw = r.target_notional / 0.06
    assert raw == pytest.approx(6666.666666, rel=1e-9)
    assert r.binding_constraint == "ALLOCATION"
    assert r.quantity == pytest.approx(6666.666666, rel=1e-6)


# 4) Available Balance = 33.35 -> Target Margin = 6.67
def test_balance_33_35_target_margin_6_67():
    r = _size(33.35, 0.06, 0.05, risk_pct=1.0)
    assert r.target_margin == pytest.approx(6.67, abs=1e-9)


# 5) DOGE @ 0.06 with 33.35 -> raw quantity 2223.333333
def test_doge_33_35_raw_quantity_2223_333333():
    r = _size(33.35, 0.06, 0.05, risk_pct=1.0)
    raw = r.target_notional / 0.06
    assert raw == pytest.approx(2223.333333, rel=1e-9)


# 6 & 7) BUY and SELL use the same size equation
def test_buy_and_sell_use_same_size_equation():
    buy = _size(100.0, 0.06, 0.05, side="BUY", risk_pct=1.0)
    sell = _size(100.0, 0.06, 0.05, side="SELL", risk_pct=1.0)
    assert buy.quantity == pytest.approx(sell.quantity)
    assert buy.final_notional == pytest.approx(sell.final_notional)
    assert buy.target_margin == pytest.approx(sell.target_margin)


# 8) No double leverage application
def test_no_double_leverage():
    r = _size(100.0, 0.06, 0.05, risk_pct=1.0)
    # notional = margin * leverage exactly once
    assert r.target_notional == pytest.approx(r.target_margin * 20)
    # quantity * entry == notional exactly once (contract_size = 1)
    assert r.quantity * 0.06 == pytest.approx(r.final_notional)


# 9) No competing allocation percentage
def test_only_20_and_30_allocation_exist():
    assert allocation_for_classification("TREND")[0] == pytest.approx(0.20)
    assert allocation_for_classification("LOW")[0] == pytest.approx(0.20)
    assert allocation_for_classification("SNIPER")[0] == pytest.approx(0.30)
    # A bare SNIPER label without authorization is downgraded to 20%.
    assert effective_sizing_classification("SNIPER", strong_tier_authorized=False) == "TREND"
    assert allocation_for_classification(
        effective_sizing_classification("SNIPER", strong_tier_authorized=False))[0] == pytest.approx(0.20)


# 10) Reduction below the allocation target is explicit, not silent
def test_reduction_below_target_is_explicit():
    r = _size(33.35, 0.06, 2.4, risk_pct=1.5)  # wide stop -> risk gate binds
    assert r.binding_constraint == "RISK_GATE"
    assert r.final_notional < r.target_notional
    assert r.margin_achieved_ratio < 1.0
    assert r.margin_required < r.target_margin


# 11) Unreadable / zero balance blocks the entry (fail closed at the sizing layer)
def test_zero_or_unreadable_balance_rejected():
    with pytest.raises(ValueError):
        _size(0.0, 0.06, 0.05)


# 12) Stale cache is not reused (snapshot helper honours max age)
def test_stale_balance_cache_not_reused():
    import ggpbot
    ggpbot.PAPER_MODE = False
    ggpbot.CACHE["free_balance"] = {"value": 999.0, "ts": 0.0}  # ancient
    calls = {"n": 0}

    class Ex:
        def fetch_balance(self, params=None):
            calls["n"] += 1
            return {"free": {"USDT": 33.35}, "total": {"USDT": 33.35}}
    ggpbot.ex = Ex()
    bal, source, ts = ggpbot.get_available_balance_snapshot(max_age_sec=1.0)
    assert calls["n"] == 1, "a stale cache entry must trigger a fresh fetch"
    assert bal == pytest.approx(33.35)
    assert source == "BINGX_FUTURES_FREE_USDT"
    ggpbot.PAPER_MODE = True
    ggpbot.CACHE.pop("free_balance", None)


# 13) Contract size and precision are applied correctly
def test_contract_size_and_precision_applied():
    r = _size(100.0, 0.06, 0.05, contract_size=10.0, risk_pct=1.0)
    # quantity is in contracts: notional = quantity * contract_size * entry
    assert r.quantity * r.contract_size * 0.06 == pytest.approx(r.final_notional)
    assert r.final_notional == pytest.approx(r.target_notional)


# 14) Risk gate prevents any size beyond the approved loss budget
def test_risk_gate_caps_loss():
    for stop_pct in (0.5, 1.0, 2.4, 5.0, 10.0):
        r = _size(100.0, 0.06, stop_pct, risk_pct=1.5)
        assert r.estimated_risk_pct <= 1.5 + 1e-9
        assert r.estimated_loss_at_stop <= r.max_risk_usdt * 1.000001


# 15) LIVE order without a verified native stop is not the sizing layer's call,
#     but the sizing layer must never silently shrink; verify it always reports.
def test_sizing_always_reports_binding_constraint():
    for stop_pct in (0.2, 1.0, 3.0, 8.0):
        r = _size(50.0, 0.06, stop_pct, risk_pct=1.0)
        assert r.binding_constraint in {"ALLOCATION", "RISK_GATE"}
        assert 0.0 < r.margin_achieved_ratio <= 1.0 + 1e-9


# 16) Executed quantity consistency: the quantity sent equals notional / (price*cs)
def test_quantity_matches_notional_definition():
    r = _size(100.0, 0.06, 1.0, risk_pct=1.0, contract_size=1.0)
    recomputed = r.final_notional / (0.06 * 1.0)
    assert r.quantity == pytest.approx(recomputed, rel=1e-9)


# Mandatory DOGE examples, allocation-binding (tight stop) case
def test_doge_example_100_buy_allocation_binding():
    r = _size(100.0, 0.06, 0.05, side="BUY", risk_pct=1.0)
    assert r.target_margin == pytest.approx(20.0)
    assert r.target_notional == pytest.approx(400.0)
    assert r.quantity == pytest.approx(6666.666666, rel=1e-6)


def test_doge_example_33_35_sell_allocation_binding():
    r = _size(33.35, 0.06, 0.05, side="SELL", risk_pct=1.0)
    assert r.target_margin == pytest.approx(6.67, abs=1e-9)
    assert r.target_notional == pytest.approx(133.40, abs=1e-9)
    assert r.quantity == pytest.approx(2223.333333, rel=1e-6)


# --------------------------------------------------------------------------- #
# Integration: the REAL execute_entry sizing path emits [POSITION_SIZING_AUDIT]
# proving the 20% allocation reached the order, and BUY/SELL use the same size.
# --------------------------------------------------------------------------- #
def _run_execute_entry(monkeypatch, *, side, balance, price=0.06, stop_pct=0.05):
    """Drive ggpbot.execute_entry in PAPER mode and return captured logs + STATE."""
    import numpy as np
    import pandas as pd
    import ggpbot

    logs = []
    monkeypatch.setattr(ggpbot, "PAPER_MODE", True)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", False)
    ggpbot.paper["balance"] = balance
    ggpbot.paper["position"] = None
    for key in ("open", "in_position"):
        if key in ggpbot.STATE:
            ggpbot.STATE[key] = False
    ggpbot.STATE["open"] = False
    ggpbot.TRADE_STATE["in_position"] = False
    ggpbot._ACTIVE_TRADE = False

    # Realistic OHLCV frame so ADX/DI/structure engines have data.
    n = 120
    ts = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    base = np.linspace(0.055, price, n)
    frame = pd.DataFrame({
        "timestamp": ts, "open": base * 0.999, "high": base * 1.004,
        "low": base * 0.996, "close": base, "volume": np.full(n, 1000.0),
    })
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", lambda *a, **k: frame)
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: price)
    monkeypatch.setattr(ggpbot, "update_position_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(ggpbot, "tg_entry", lambda *a, **k: None)
    monkeypatch.setattr(ggpbot, "_live_manager", type("M", (), {"start_trade": lambda *a, **k: None})())

    real_log = ggpbot.log_execution

    def capture(msg, level="INFO", *a, **k):
        logs.append(str(msg))
        return real_log(msg, level, *a, **k)
    monkeypatch.setattr(ggpbot, "log_execution", capture)

    stop = price * (1 - stop_pct / 100.0) if side == "BUY" else price * (1 + stop_pct / 100.0)
    tp1 = price * (1 + 0.008) if side == "BUY" else price * (1 - 0.008)
    tp2 = price * (1 + 0.02) if side == "BUY" else price * (1 - 0.02)
    ok = ggpbot.execute_entry(side, "DOGE/USDT", price, stop, tp1, tp2, 80,
                              "test", 0.001, "TREND", "TEST", "TREND")
    return ok, logs, ggpbot.STATE


def test_execute_entry_audit_proves_20pct_reached_the_order(monkeypatch):
    ok, logs, state = _run_execute_entry(monkeypatch, side="BUY", balance=100.0)
    audit = [m for m in logs if "POSITION_SIZING_AUDIT" in m]
    assert audit, "the position sizing audit block must be emitted"
    block = audit[-1]
    assert "BingX Available Balance: 100.0000 USDT" in block
    assert "Allocation Percentage: 20%" in block
    assert "Target Margin: 20.0000 USDT" in block
    assert "Leverage: 20x" in block
    assert "Target Notional: 400.0000 USDT" in block
    assert "Binding Constraint: ALLOCATION" in block
    assert "Final Status: PASS" in block
    assert ok is True


def test_execute_entry_buy_and_sell_same_quantity(monkeypatch):
    ok_b, logs_b, state_b = _run_execute_entry(monkeypatch, side="BUY", balance=100.0)
    qty_b = state_b.get("qty")
    ok_s, logs_s, state_s = _run_execute_entry(monkeypatch, side="SELL", balance=100.0)
    qty_s = state_s.get("qty")
    assert ok_b and ok_s
    assert qty_b and qty_s
    assert qty_b == pytest.approx(qty_s, rel=1e-9), "direction must not change the size"


def test_execute_entry_risk_gate_logged_not_silent(monkeypatch):
    # Wide stop: the risk gate binds and the reduction must be logged explicitly.
    ok, logs, state = _run_execute_entry(monkeypatch, side="BUY", balance=33.35, stop_pct=2.4)
    assert any("SIZING_RISK_GATE" in m for m in logs), "a risk-gate reduction must be logged"
    audit = [m for m in logs if "POSITION_SIZING_AUDIT" in m]
    assert audit and "Binding Constraint: RISK_GATE" in audit[-1]
