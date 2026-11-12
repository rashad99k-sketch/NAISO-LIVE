"""Regression tests for the Sniper V2 integration boundary (no exchange calls)."""
import math
import pandas as pd
import pytest
import ggpbot


def _frame(n=20):
    ts = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    close = [100.0 + math.sin(i / 3.0) for i in range(n)]
    return pd.DataFrame({
        "timestamp": ts,
        "open": [x - 0.1 for x in close],
        "high": [x + 0.5 for x in close],
        "low": [x - 0.5 for x in close],
        "close": close,
        "volume": [1000.0] * n,
    })


def test_sniper_v2_tp_levels_are_fixed_move_percentages():
    assert ggpbot.SNIPER_V2_TP1_MOVE_PCT == pytest.approx(0.40)
    assert ggpbot.SNIPER_V2_TP2_MOVE_PCT == pytest.approx(1.00)


def test_pivot_high_waits_for_three_right_candles():
    frame = _frame(20)
    frame.loc[16, "high"] = 110.0
    frame.loc[17, "high"] = 105.0
    frame.loc[18, "high"] = 104.0
    frame.loc[19, "high"] = 103.0
    assert ggpbot.is_pivot_high(frame, lookback=3)
    frame.loc[19, "high"] = 111.0
    assert not ggpbot.is_pivot_high(frame, lookback=3)


def test_pivot_low_waits_for_three_right_candles():
    frame = _frame(20)
    frame.loc[16, "low"] = 90.0
    frame.loc[17, "low"] = 95.0
    frame.loc[18, "low"] = 96.0
    frame.loc[19, "low"] = 97.0
    assert ggpbot.is_pivot_low(frame, lookback=3)
    frame.loc[19, "low"] = 89.0
    assert not ggpbot.is_pivot_low(frame, lookback=3)


def test_sniper_v2_pauses_when_position_open(monkeypatch):
    monkeypatch.setattr(ggpbot, "SNIPER_V2_ENABLED", True)
    monkeypatch.setitem(ggpbot.STATE, "open", True)
    assert ggpbot.sniper_engine_v2() is False
    monkeypatch.setitem(ggpbot.STATE, "open", False)


def test_sniper_v2_uses_twenty_percent_wallet_target_at_twenty_x():
    from ggp_position_sizing import allocation_for_classification, calculate_position_size

    allocation, reason = allocation_for_classification("SNIPER_V2")
    assert allocation == pytest.approx(0.20)
    assert reason == "STANDARD_20_PERCENT_MARGIN"

    sizing = calculate_position_size(
        classification="SNIPER_V2",
        available_balance=100.0,
        entry_price=100.0,
        stop_price=99.9,
        side="BUY",
        leverage=20,
        contract_size=1.0,
        max_trade_risk_pct=10.0,
        stop_execution_buffer_pct=0.0,
    )
    assert sizing.target_margin == pytest.approx(20.0)
    assert sizing.target_notional == pytest.approx(400.0)
    assert sizing.margin_required == pytest.approx(20.0)
    assert sizing.final_notional == pytest.approx(400.0)


def test_sniper_v2_risk_gate_can_still_reduce_target_position():
    from ggp_position_sizing import calculate_position_size

    sizing = calculate_position_size(
        classification="SNIPER_V2",
        available_balance=100.0,
        entry_price=100.0,
        stop_price=95.0,
        side="BUY",
        leverage=20,
        contract_size=1.0,
        max_trade_risk_pct=10.0,
        stop_execution_buffer_pct=0.0,
    )
    assert sizing.target_margin == pytest.approx(20.0)
    assert sizing.binding_constraint == "RISK_GATE"
    assert sizing.margin_required < sizing.target_margin


def _closed_zone_frame(ts_base, *, previous_close=100.0, last_open=100.0,
                       last_high=100.5, last_low=99.5, last_close=100.0):
    ts = [ts_base + i * 900_000 for i in range(80)]
    close = [100.0] * 80
    open_ = [100.0] * 80
    high = [100.5] * 80
    low = [99.5] * 80
    close[-2] = previous_close
    open_[-2] = previous_close
    close[-1] = last_close
    open_[-1] = last_open
    high[-1] = last_high
    low[-1] = last_low
    return pd.DataFrame({
        "timestamp": ts, "open": open_, "high": high,
        "low": low, "close": close, "volume": [1000.0] * 80,
    })


def test_sniper_v2_waits_for_departure_then_a_later_retest(monkeypatch):
    monkeypatch.setattr(ggpbot, "roro_canonical_symbol", lambda value: str(value))
    monkeypatch.setattr(ggpbot, "compute_atr", lambda frame, period=14: pd.Series(
        [1.0] * len(frame), index=frame.index
    ))
    ggpbot.SNIPER_ZONES.clear()
    ggpbot.SNIPER_V2_LAST_PIVOT_TS.clear()

    departure = _closed_zone_frame(
        1_700_000_000_000, previous_close=99.2,
        last_open=99.0, last_high=99.0, last_low=98.5, last_close=98.8,
    )
    zone = {
        "symbol": "BTC/USDT", "canonical_symbol": "BTC/USDT", "type": "TOP",
        "high": 100.0, "low": 99.5, "created_at": __import__("time").time(),
        "last_candle_ts": int(departure.timestamp.iloc[-2]), "state": "WAIT",
        "departed": False,
    }
    ggpbot.SNIPER_ZONES["BTC/USDT"] = zone
    updated = ggpbot._sniper_v2_update_zone("BTC/USDT", departure)
    assert updated["state"] == "WAIT"
    assert updated["departed"] is True

    retest = _closed_zone_frame(
        int(departure.timestamp.iloc[-1]) + 900_000,
        previous_close=99.2, last_open=99.8,
        last_high=100.0, last_low=99.4, last_close=99.7,
    )
    updated = ggpbot._sniper_v2_update_zone("BTC/USDT", retest)
    assert updated["state"] == "READY"
    assert updated["retest_candle_ts"] == int(retest.timestamp.iloc[-1])
    assert ggpbot._sniper_v2_confirmation_due(updated, int(retest.timestamp.iloc[-1])) is False
    assert ggpbot._sniper_v2_confirmation_due(
        updated, int(retest.timestamp.iloc[-1]) + 900_000
    ) is True


def test_sniper_v2_confirmation_requires_rejection_after_retest():
    frame = _closed_zone_frame(
        1_700_000_000_000, previous_close=99.8,
        last_open=99.8, last_high=100.2, last_low=99.4, last_close=99.6,
    )
    zone = {"type": "TOP", "high": 100.0, "low": 99.5}
    confirmed, checks = ggpbot.check_sniper_confirmation(frame, "TOP", zone)
    assert confirmed is True
    assert checks >= 2


def test_sniper_v2_structural_stop_stays_beyond_the_pivot_zone():
    top = {"low": 99.5, "high": 100.0}
    bottom = {"low": 99.0, "high": 99.5}
    assert ggpbot._sniper_v2_structural_stop("SELL", top, 2.0, 100.5) == pytest.approx(100.8)
    assert ggpbot._sniper_v2_structural_stop("BUY", bottom, 2.0, 98.5) == pytest.approx(98.2)


def test_sniper_v2_sizing_uses_total_wallet_and_only_uses_free_as_feasibility():
    total, free, source = ggpbot._sniper_v2_wallet_balances(
        wallet_snapshot={"total": {"USDT": 100.0}, "free": {"USDT": 7.0}},
        paper_mode=False,
    )
    assert total == pytest.approx(100.0)
    assert free == pytest.approx(7.0)
    assert source == "BINGX_WALLET_TOTAL_USDT"


def test_sniper_v2_paper_wallet_balance_is_used_as_total():
    total, free, source = ggpbot._sniper_v2_wallet_balances(
        paper_wallet_balance=25.0, paper_mode=True,
    )
    assert total == pytest.approx(25.0)
    assert free == pytest.approx(25.0)
    assert source == "PAPER_WALLET_TOTAL"


def test_sniper_v2_watchlist_scan_rotates_across_universe(monkeypatch):
    monkeypatch.setattr(ggpbot, "roro_exchange_symbol", lambda value: str(value))
    monkeypatch.setattr(ggpbot, "roro_canonical_symbol", lambda value: str(value))
    monkeypatch.setattr(ggpbot, "SNIPER_V2_MAX_SYMBOLS", 2)
    monkeypatch.setattr(ggpbot, "_SNIPER_V2_SYMBOL_CURSOR", 0)
    now = __import__("time").time()
    monkeypatch.setitem(ggpbot.MEMORY, "watchlist", {
        f"COIN{i}/USDT": {
            "symbol": f"COIN{i}/USDT", "score": float(100 - i),
            "decision_state": "WAITING_FOR_RETEST", "last_update": now,
        }
        for i in range(5)
    })
    first = ggpbot._sniper_v2_symbols()
    second = ggpbot._sniper_v2_symbols()
    assert len(first) == 2
    assert len(second) == 2
    assert set(first).isdisjoint(set(second))


def test_sniper_v2_wallet_balance_fails_closed_when_snapshot_missing():
    with pytest.raises(ValueError, match="SNIPER_WALLET_BALANCE_UNAVAILABLE"):
        ggpbot._sniper_v2_wallet_balances(wallet_snapshot=None, paper_mode=False)



def _entry_test_frame(now_ms, n=100, *, latest_closed_intervals_ago=0, forming=False):
    tf_ms = 900_000
    current_bucket = int(now_ms // tf_ms)
    last_closed_open = (current_bucket - 1 - int(latest_closed_intervals_ago)) * tf_ms
    timestamps = [last_closed_open - (n - 1 - i) * tf_ms for i in range(n)]
    prices = [100.0 + i * 0.01 for i in range(n)]
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": [p + 0.5 for p in prices],
        "low": [p - 0.5 for p in prices],
        "close": [p + 0.1 for p in prices],
        "volume": [1000.0] * n,
    })
    if forming:
        row = frame.iloc[[-1]].copy()
        row["timestamp"] = current_bucket * tf_ms
        frame = pd.concat([frame, row], ignore_index=True)
    return frame


def test_institutional_entry_reuses_same_authorized_closed_frame(monkeypatch):
    now_ms = int(__import__("time").time() * 1000)
    frame = _entry_test_frame(now_ms)
    monkeypatch.setattr(ggpbot, "_entry_exchange_time_ms", lambda: (now_ms, "TEST_EXCHANGE"))
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not refetch shared frame")))
    context = {
        "exchange_symbol": "BTC/USDT:USDT",
        "candle_timeframe": "15m",
        "closed_candle_frame": frame,
        "closed_candle_source": "SNIPER_V2_CONFIRMED_FRAME",
    }
    checked, diagnostic = ggpbot.resolve_entry_closed_candles("BTC/USDT:USDT", context)
    assert checked is not None
    assert diagnostic["reason"] == "VALID"
    assert diagnostic["source"] == "SNIPER_V2_CONFIRMED_FRAME"
    assert int(checked.timestamp.iloc[-1]) == int(frame.timestamp.iloc[-1])


def test_entry_closed_frame_classifies_only_forming_candle():
    now_ms = int(__import__("time").time() * 1000)
    tf_ms = 900_000
    current_open = (now_ms // tf_ms) * tf_ms
    frame = pd.DataFrame({
        "timestamp": [current_open], "open": [100.0], "high": [101.0],
        "low": [99.0], "close": [100.5], "volume": [1000.0],
    })
    checked, diagnostic = ggpbot._validate_entry_closed_frame(
        "BTC/USDT:USDT", frame,
        {"exchange_symbol": "BTC/USDT:USDT", "candle_timeframe": "15m"},
        now_ms=now_ms, time_source="TEST_EXCHANGE",
    )
    assert checked is None
    assert diagnostic["reason"] == "ONLY_FORMING_CANDLE"


def test_entry_closed_frame_classifies_insufficient_history():
    now_ms = int(__import__("time").time() * 1000)
    frame = _entry_test_frame(now_ms, n=60)
    checked, diagnostic = ggpbot._validate_entry_closed_frame(
        "BTC/USDT:USDT", frame,
        {"exchange_symbol": "BTC/USDT:USDT", "candle_timeframe": "15m"},
        now_ms=now_ms, time_source="TEST_EXCHANGE",
    )
    assert checked is None
    assert diagnostic["reason"] == "INSUFFICIENT_CLOSED_BARS"


def test_entry_closed_frame_classifies_stale_market_data():
    now_ms = int(__import__("time").time() * 1000)
    frame = _entry_test_frame(now_ms, latest_closed_intervals_ago=3)
    checked, diagnostic = ggpbot._validate_entry_closed_frame(
        "BTC/USDT:USDT", frame,
        {"exchange_symbol": "BTC/USDT:USDT", "candle_timeframe": "15m"},
        now_ms=now_ms, time_source="TEST_EXCHANGE",
    )
    assert checked is None
    assert diagnostic["reason"] == "CLOSED_CANDLE_STALE"


def test_entry_closed_frame_rejects_symbol_or_timeframe_mismatch():
    now_ms = int(__import__("time").time() * 1000)
    frame = _entry_test_frame(now_ms)
    wrong_symbol, symbol_diag = ggpbot._validate_entry_closed_frame(
        "BTC/USDT:USDT", frame,
        {"exchange_symbol": "ETH/USDT:USDT", "candle_timeframe": "15m"},
        now_ms=now_ms, time_source="TEST_EXCHANGE",
    )
    wrong_tf, tf_diag = ggpbot._validate_entry_closed_frame(
        "BTC/USDT:USDT", frame,
        {"exchange_symbol": "BTC/USDT:USDT", "candle_timeframe": "1h"},
        now_ms=now_ms, time_source="TEST_EXCHANGE",
    )
    assert wrong_symbol is None and symbol_diag["reason"] == "SYMBOL_TIMEFRAME_MISMATCH"
    assert wrong_tf is None and tf_diag["reason"] == "SYMBOL_TIMEFRAME_MISMATCH"


def test_entry_data_rejection_populates_dashboard_why_no_entry():
    before = len(ggpbot.MEMORY.get("no_entry_feed", []))
    ggpbot.record_entry_data_rejection(
        "BTC/USDT:USDT", "SELL",
        {"reason": "OHLCV_RATE_LIMITED", "detail": "test rate limit"},
    )
    item = ggpbot.MEMORY["no_entry_feed"][-1]
    assert len(ggpbot.MEMORY["no_entry_feed"]) == before + 1
    assert item["category"] == "OHLCV_RATE_LIMITED"
    assert item["stage"] == "INSTITUTIONAL_ENTRY_GATE"
    assert "OHLCV_RATE_LIMITED" in item["reason"]
    assert ggpbot.MEMORY["entry_rejection_metrics"]["OHLCV_RATE_LIMITED"] >= 1
