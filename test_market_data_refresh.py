"""Market-data refresh + indicator integrity regression (open-trade management).

Proves the root-cause fix: the live management path requested fewer OHLCV candles
than the fetcher minimum, so indicators never refreshed after entry. Also covers
CONFIRMED vs LIVE snapshot separation, freshness detection, stale/duplicate feeds,
and that a data problem never touches Native SL / TP1 / TP2.
"""
import os
import time

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("BINGX_API_KEY", "")
os.environ.setdefault("BINGX_API_SECRET", "")

import ggpbot
from ggp_market_data import (
    FETCH_MIN_CANDLES,
    MANAGEMENT_OHLCV_LIMIT,
    REQUIRED_CLOSED_CANDLES,
    MarketDataRefresh,
    split_closed_and_current,
    timeframe_seconds,
)


def _frame(n, *, latest_closed_offset=-1200.0, include_forming=False,
           advance_intervals=0, start=83.0, step=0.01, interval_ms=900_000):
    """Build an OHLCV frame of `n` CLOSED candles (optionally + 1 forming candle).

    The most recent CLOSED candle is aligned so that its open time is in the past
    by at least one full interval, which makes "is the last candle still forming?"
    deterministic regardless of wall-clock position within the interval.
    `advance_intervals` shifts every timestamp forward by that many intervals.
    """
    interval_s = interval_ms / 1000.0
    now = time.time()
    # Anchor to the START of the interval that began before `now - latest_closed_offset`.
    anchor_open_s = now + latest_closed_offset + advance_intervals * interval_s
    # Round DOWN to the interval boundary, then step one full interval back so the
    # newest candle is unambiguously closed.
    last_closed_open_s = (int(anchor_open_s // interval_s) - 1) * interval_s
    last_closed_open_ms = int(last_closed_open_s * 1000)
    ts = [last_closed_open_ms - (n - 1 - i) * interval_ms for i in range(n)]
    base = np.linspace(start, start + step * n, n)
    frame = pd.DataFrame({
        "timestamp": ts, "open": base * 0.999, "high": base * 1.002,
        "low": base * 0.998, "close": base, "volume": np.full(n, 1000.0),
    })
    if include_forming:
        # Place the forming candle at the CURRENT interval boundary so
        # `now < open + duration` holds (it is genuinely still forming).
        forming_ms = int(now // interval_s) * interval_ms
        if forming_ms <= last_closed_open_ms:
            forming_ms = last_closed_open_ms + interval_ms
        extra = frame.iloc[[-1]].copy()
        extra["timestamp"] = forming_ms
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def _calc(closed):
    adx = ggpbot.compute_adx(closed)
    p, m, a, s = ggpbot.get_di_components(closed)
    return {"adx": float(a) if a is not None else 0.0,
            "di_plus": float(p) if p is not None else 0.0,
            "di_minus": float(m) if m is not None else 0.0,
            "close": float(closed["close"].iloc[-1])}


# 1) The management path must request at least the fetcher minimum.
def test_management_ohlcv_limit_meets_fetch_minimum():
    assert MANAGEMENT_OHLCV_LIMIT >= FETCH_MIN_CANDLES


# 2) fetch_ohlcv rejects < 100 candles (the original silent freeze trigger).
def test_fetch_ohlcv_rejects_below_minimum():
    rows = _frame(80).values.tolist()
    ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(lambda sym, tf, limit=None: rows)})()
    ggpbot.PAPER_MODE = False
    try:
        assert ggpbot.fetch_ohlcv("SOL/USDT", 80) is None
        rows2 = _frame(120).values.tolist()
        ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(lambda sym, tf, limit=None: rows2)})()
        assert len(ggpbot.fetch_ohlcv("SOL/USDT", 120)) == 120
    finally:
        ggpbot.PAPER_MODE = True


# 3) get_ohlcv_safe with the management limit returns a usable frame.
def test_get_ohlcv_safe_returns_frame_at_management_limit():
    rows = _frame(MANAGEMENT_OHLCV_LIMIT, latest_closed_offset=-1200.0).values.tolist()
    ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(lambda sym, tf, limit=None: rows)})()
    ggpbot.PAPER_MODE = False
    ggpbot.CACHE.clear()
    try:
        df = ggpbot.get_ohlcv_safe("SOL/USDT", MANAGEMENT_OHLCV_LIMIT)
        assert df is not None and len(df) >= REQUIRED_CLOSED_CANDLES
    finally:
        ggpbot.PAPER_MODE = True
        ggpbot.CACHE.clear()


# 4) Closed/current split: a forming candle is separated from closed ones.
def test_split_closed_and_current_separates_forming_candle():
    frame = _frame(120, latest_closed_offset=-1200.0, include_forming=True)  # last candle just opened -> forming
    closed, current, last_closed, current_ts = split_closed_and_current(frame, "15m")
    assert current is not None
    assert current_ts is not None
    assert len(closed) == len(frame) - 1
    assert last_closed is not None


def test_split_when_last_candle_is_closed():
    # Last candle opened 20 minutes ago (> 15m) -> already closed.
    frame = _frame(120, latest_closed_offset=-1200.0)
    closed, current, last_closed, current_ts = split_closed_and_current(frame, "15m")
    assert current is None
    assert current_ts is None
    assert len(closed) == len(frame)


# 5) CONFIRMED snapshot changes only when a new closed candle appears.
def test_confirmed_snapshot_updates_only_on_new_closed_candle():
    # Start with the newest candle opened ~32 min ago, then advance one 15m
    # interval so the newest candle is still already closed (opened ~17 min ago).
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    refresh = MarketDataRefresh(fetch_fn=lambda s, l: frames["f"], timeframe="15m")
    st1 = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st1["ohlcv_freshness"] == "FRESH"
    v1 = st1["snapshot_version"]
    # Same closed candle again -> UNCHANGED_BUT_VALID, version unchanged.
    st2 = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st2["ohlcv_freshness"] == "UNCHANGED_BUT_VALID"
    assert st2["snapshot_version"] == v1
    # A new closed candle -> FRESH, version increments.
    frames["f"] = _frame(120, latest_closed_offset=-1200.0, advance_intervals=2)
    st3 = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st3["ohlcv_freshness"] == "FRESH"
    assert st3["snapshot_version"] > v1


# 6) INSUFFICIENT data does not publish new confirmed indicators.
def test_insufficient_candles_does_not_publish_indicators():
    refresh = MarketDataRefresh(fetch_fn=lambda s, l: _frame(40, latest_closed_offset=-1200.0), timeframe="15m")
    st = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st["ohlcv_freshness"] == "INSUFFICIENT"
    assert "confirmed_indicators" not in st
    assert refresh.confirmed is None


# 7) LIVE/PROVISIONAL snapshot is produced separately and never replaces confirmed.
def test_live_snapshot_is_separate_from_confirmed():
    frames = {"f": _frame(120, latest_closed_offset=-1200.0, include_forming=True)}
    refresh = MarketDataRefresh(fetch_fn=lambda s, l: frames["f"], timeframe="15m")
    live_calls = {"n": 0}

    def live_calc(closed, current):
        live_calls["n"] += 1
        return {"adx": 99.0, "provisional": True}

    st = refresh.refresh("SOL/USDT", calc_indicators=_calc, calc_live_indicators=live_calc)
    assert "live_indicators" in st
    assert st["live_indicators"]["provisional"] is True
    # The confirmed snapshot must still hold the confirmed ADX, not 99.
    assert refresh.confirmed is not None
    assert refresh.confirmed.indicators["adx"] != 99.0
    assert refresh.confirmed.snapshot_type == "CONFIRMED"


# 8) Stale feed (closed candle goes backwards) is detected, not published.
def test_stale_feed_detected():
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    refresh = MarketDataRefresh(fetch_fn=lambda s, l: frames["f"], timeframe="15m")
    refresh.refresh("SOL/USDT", calc_indicators=_calc)
    v1 = refresh.confirmed.snapshot_version
    frames["f"] = _frame(120, latest_closed_offset=-1200.0 - 3600.0)  # older candles
    st = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st["ohlcv_freshness"] == "STALE"
    assert refresh.confirmed.snapshot_version == v1  # not advanced


# 9) timeframe parsing is correct (guards against a wrong-timeframe fetch).
def test_timeframe_seconds_parsing():
    assert timeframe_seconds("15m") == 900
    assert timeframe_seconds("1h") == 3600
    assert timeframe_seconds("4h") == 14400
    assert timeframe_seconds("1d") == 86400


# 10) Required candle count has a safe margin over the longest warm-up.
def test_required_closed_candles_has_margin():
    # EMA50 + ADX(14) double smoothing; 60 is a safe floor.
    assert REQUIRED_CLOSED_CANDLES >= 55


# 11) Full management cycle: indicators refresh and reach STATE across cycles.
def _seed_open_position(monkeypatch, balance=100.0):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", True)
    ggpbot.STATE.update({
        "open": True, "side": "BUY", "current_symbol": "SOL/USDT:USDT", "trade_id": "T1",
        "entry": 83.36, "qty": 1.0, "initial_qty": 1.0, "remaining_qty": 1.0,
        "mark_price": 83.41, "roe_pct": 1.23, "native_sl_verified": True,
        "native_sl_order_id": "sl-1", "synthetic_sl": 82.0, "tp1_price": 84.0,
        "tp2_price": 85.0, "atr": 0.1, "max_price": 83.41, "min_price": 83.36,
        "position_mode": "HEDGE", "position_side": "LONG",
        "trade_thesis": {"thesis_strength": 5, "entry_price": 83.36, "side": "BUY"},
        "adx_live": 0.0, "di_plus_live": 0.0, "di_minus_live": 0.0,
        "continuation_probability": 0.5, "market_regime": "UNKNOWN",
        "last_management_closed_candle_ts": 0.0, "indicator_snapshot_version": 0,
        "last_indicator_calc_closed_ts": 0.0, "last_market_data_freshness": "UNKNOWN",
        "trade_state": "RANGE_CHOP", "current_confidence": 50.0,
    })
    ggpbot.TRADE_STATE["in_position"] = True


def test_management_cycle_refreshes_indicators_and_reaches_state(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", lambda *a, **k: (frames["f"], "FRESH_REQUEST"))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    logs = []
    real_log = ggpbot.log_execution
    monkeypatch.setattr(ggpbot, "log_execution",
                        lambda m, lvl="INFO", *a, **k: (logs.append(str(m)), real_log(m, lvl, *a, **k))[1])

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())

    assert any("MARKET_DATA_REFRESH" in m for m in logs)
    assert any("INDICATOR_REFRESH" in m for m in logs)
    assert any("PROFIT_BRAIN_DATA_VALIDATION" in m for m in logs)
    # Indicators actually reached STATE (not frozen at entry defaults).
    assert ggpbot.STATE["adx_live"] > 0
    assert ggpbot.STATE["di_plus_live"] > 0
    assert ggpbot.STATE["continuation_probability"] is not None
    assert int(ggpbot.STATE.get("indicator_snapshot_version", 0)) >= 1


def test_management_cycle_second_call_same_candle_keeps_version(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", lambda *a, **k: (frames["f"], "FRESH_REQUEST"))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    v1 = int(ggpbot.STATE.get("indicator_snapshot_version", 0))
    # Same closed candle, 5 seconds later -> version must NOT advance.
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time() + 5)
    v2 = int(ggpbot.STATE.get("indicator_snapshot_version", 0))
    assert v2 == v1


def test_management_cycle_insufficient_data_keeps_protection(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    ggpbot.STATE["synthetic_sl"] = 82.0
    ggpbot.STATE["native_sl_verified"] = True
    # Fetcher returns too few candles -> the management path must bail safely.
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", lambda *a, **k: (_frame(40, latest_closed_offset=-1200.0), "FRESH_REQUEST"))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    logs = []
    real_log = ggpbot.log_execution
    monkeypatch.setattr(ggpbot, "log_execution",
                        lambda m, lvl="INFO", *a, **k: (logs.append(str(m)), real_log(m, lvl, *a, **k))[1])

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    assert any("INSUFFICIENT" in m for m in logs)
    # Native SL and the trade remain intact.
    assert ggpbot.STATE["native_sl_verified"] is True
    assert ggpbot.STATE["synthetic_sl"] == 82.0
    assert ggpbot.STATE["open"] is True


# 12) No double indicator math / no stale-value masquerade: freshness is explicit.
def test_freshness_labels_are_explicit():
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    refresh = MarketDataRefresh(fetch_fn=lambda s, l: frames["f"], timeframe="15m")
    st1 = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    st2 = refresh.refresh("SOL/USDT", calc_indicators=_calc)
    assert st1["ohlcv_freshness"] == "FRESH"
    assert st2["ohlcv_freshness"] == "UNCHANGED_BUT_VALID"
    # Both are valid; neither claims to be a brand-new candle.
    assert refresh.confirmed.data_freshness in {"FRESH", "UNCHANGED_BUT_VALID"}


# 13) Rate-limit safety: the management cycle uses the shared cache (no per-cycle
#     extra fetch storm). Two back-to-back cycles reuse one cached frame.
def test_management_cycle_reuses_cache_within_ttl(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    calls = {"n": 0}

    def fake_fetch(sym, tf, limit=None):
        calls["n"] += 1
        return _frame(MANAGEMENT_OHLCV_LIMIT, latest_closed_offset=-1200.0).values.tolist()
    ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(fake_fetch)})()
    ggpbot.CACHE.clear()
    ggpbot.STATE["open"] = True
    try:
        ggpbot.get_ohlcv_safe("SOL/USDT:USDT", MANAGEMENT_OHLCV_LIMIT)
        ggpbot.get_ohlcv_safe("SOL/USDT:USDT", MANAGEMENT_OHLCV_LIMIT)
        assert calls["n"] == 1, "the second call within the TTL must hit the cache"
    finally:
        ggpbot.STATE["open"] = False
        ggpbot.CACHE.clear()


# 14) Multi-cycle simulation: run several management cycles across a candle
#     boundary and confirm indicators refresh, then hold steady between candles.
def test_multi_cycle_simulation_refreshes_then_holds(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", lambda *a, **k: (frames["f"], "FRESH_REQUEST"))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE

    versions = []
    # Three cycles on the same closed candle -> version stays constant.
    for i in range(3):
        mgr.last_heavy_calc_ts = 0.0
        mgr._apply_management("SOL/USDT:USDT", time.time() + i * 5)
        versions.append(int(ggpbot.STATE.get("indicator_snapshot_version", 0)))
    assert len(set(versions)) == 1, "no new closed candle -> version must not advance"

    # A new closed candle arrives -> version advances exactly once.
    frames["f"] = _frame(120, latest_closed_offset=-1200.0, advance_intervals=2)
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time() + 20)
    assert int(ggpbot.STATE.get("indicator_snapshot_version", 0)) > versions[-1]


# 15) Native SL and TP1/TP2 are never altered by a market-data problem.
def test_data_failure_never_touches_protection(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    before = {"sl": ggpbot.STATE["synthetic_sl"], "tp1": ggpbot.STATE["tp1_price"],
              "tp2": ggpbot.STATE["tp2_price"], "sl_order": ggpbot.STATE["native_sl_order_id"]}
    # Fetch raises -> management must fail safe and preserve protection.
    def boom(*a, **k):
        raise RuntimeError("exchange down")
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", boom)
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)
    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    assert ggpbot.STATE["synthetic_sl"] == before["sl"]
    assert ggpbot.STATE["tp1_price"] == before["tp1"]
    assert ggpbot.STATE["tp2_price"] == before["tp2"]
    assert ggpbot.STATE["native_sl_order_id"] == before["sl_order"]
    assert ggpbot.STATE["native_sl_verified"] is True


# =========================================================================== #
# FINAL VERIFICATION ROUND — items 1..7
# =========================================================================== #

def _meta(holder, source="FRESH_REQUEST"):
    """Return a fetch stub that reads `holder["f"]` on every call, so reassigning
    the frame between management cycles is observed."""
    return lambda *a, **k: (holder["f"], source)


# Item 1 + 5: snapshot version increments on a NEW closed candle and Profit Brain
# receives it; version stays constant between candles.
def test_version_increments_only_on_new_closed_candle(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta(frames))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE

    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    v1 = int(ggpbot.STATE["indicator_snapshot_version"])
    ts1 = ggpbot.STATE["last_management_closed_candle_ts"]
    assert v1 >= 1 and ts1 > 0

    # Same closed candle across several cycles -> version unchanged.
    for i in range(1, 4):
        mgr.last_heavy_calc_ts = 0.0
        mgr._apply_management("SOL/USDT:USDT", time.time() + i * 5)
        assert int(ggpbot.STATE["indicator_snapshot_version"]) == v1

    # New closed candle -> version increments and the closed ts advances.
    # Shift every timestamp forward by exactly one 15m interval so the newest
    # candle is unambiguously a NEW closed candle (deterministic, wall-clock free).
    advanced = frames["f"].copy()
    advanced["timestamp"] = advanced["timestamp"] + 900_000
    frames["f"] = advanced
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time() + 30)
    v2 = int(ggpbot.STATE["indicator_snapshot_version"])
    ts2 = ggpbot.STATE["last_management_closed_candle_ts"]
    assert v2 == v1 + 1, "exactly one new closed candle -> exactly one version bump"
    assert ts2 > ts1


# Item 3: a CACHE_HIT is never reported as a fresh BingX request.
def test_cache_hit_vs_fresh_request_reported_accurately(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    calls = {"n": 0}
    frame = _frame(MANAGEMENT_OHLCV_LIMIT, latest_closed_offset=-1200.0)

    def fake_fetch(sym, tf, limit=None):
        calls["n"] += 1
        return frame.values.tolist()
    ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(fake_fetch)})()
    ggpbot.CACHE.clear()
    ggpbot.STATE["open"] = True
    try:
        f1, src1 = ggpbot.get_ohlcv_safe_meta("SOL/USDT:USDT", MANAGEMENT_OHLCV_LIMIT)
        f2, src2 = ggpbot.get_ohlcv_safe_meta("SOL/USDT:USDT", MANAGEMENT_OHLCV_LIMIT)
        assert src1 == "FRESH_REQUEST", "first call must hit the exchange"
        assert src2 == "CACHE_HIT", "second call within TTL must be a cache hit"
        assert calls["n"] == 1, "a cache hit must NOT issue a new BingX request"
    finally:
        ggpbot.STATE["open"] = False
        ggpbot.CACHE.clear()


# Item 2 + 6: a stale cache entry is expired and refetched, never presented FRESH.
def test_expired_cache_is_refetched_not_served_stale(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    calls = {"n": 0}
    frame = _frame(MANAGEMENT_OHLCV_LIMIT, latest_closed_offset=-1200.0)

    def fake_fetch(sym, tf, limit=None):
        calls["n"] += 1
        return frame.values.tolist()
    ggpbot.ex = type("E", (), {"fetch_ohlcv": staticmethod(fake_fetch)})()
    ggpbot.CACHE.clear()
    ggpbot.STATE["open"] = True
    try:
        # Seed an ANCIENT cache entry (> any TTL).
        key = f"ohlcv_SOL/USDT:USDT_{ggpbot.INTERVAL}_{MANAGEMENT_OHLCV_LIMIT}"
        ggpbot.CACHE[key] = {"value": frame, "ts": 0.0}
        got, src = ggpbot.get_ohlcv_safe_meta("SOL/USDT:USDT", MANAGEMENT_OHLCV_LIMIT)
        assert src == "FRESH_REQUEST", "an expired cache entry must be refetched"
        assert calls["n"] == 1
        assert got is not None
    finally:
        ggpbot.STATE["open"] = False
        ggpbot.CACHE.clear()


# Item 6: BingX API failure -> freshness INVALID, decision not allowed, protection kept.
def test_api_failure_marks_invalid_and_blocks_decision(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    ggpbot.STATE["synthetic_sl"] = 82.0
    ggpbot.STATE["native_sl_verified"] = True

    def boom(*a, **k):
        raise RuntimeError("BingX 503")
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", boom)
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    logs = []
    real = ggpbot.log_execution
    monkeypatch.setattr(ggpbot, "log_execution",
                        lambda m, lvl="INFO", *a, **k: (logs.append(str(m)), real(m, lvl, *a, **k))[1])

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    assert ggpbot.STATE["last_market_data_freshness"] == "INVALID"
    assert any("fetch error" in m for m in logs)
    # Protection is untouched.
    assert ggpbot.STATE["synthetic_sl"] == 82.0
    assert ggpbot.STATE["native_sl_verified"] is True
    assert ggpbot.STATE["tp1_price"] == 84.0 and ggpbot.STATE["tp2_price"] == 85.0


# Item 1: Profit Brain receives the new snapshot; the validation log proves it.
def test_profit_brain_receives_new_snapshot_log(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frames = {"f": _frame(120, latest_closed_offset=-1200.0)}
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta(frames))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    logs = []
    real = ggpbot.log_execution
    monkeypatch.setattr(ggpbot, "log_execution",
                        lambda m, lvl="INFO", *a, **k: (logs.append(str(m)), real(m, lvl, *a, **k))[1])

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    validation = [m for m in logs if "PROFIT_BRAIN_DATA_VALIDATION" in m]
    assert validation, "the validation log must be emitted"
    block = validation[-1]
    assert "Decision Allowed: True" in block
    assert "Data Freshness: FRESH" in block
    assert "Missing Indicators: []" in block
    refresh = [m for m in logs if "MARKET_DATA_REFRESH" in m][-1]
    assert "Data Source: FRESH_REQUEST" in refresh


# =========================================================================== #
# ENFORCED DECISION GATE — board.evaluate must not run on invalid market data
# =========================================================================== #

class _BoardSpy:
    """Wrap the real board so we can count evaluate() calls."""
    def __init__(self, real):
        self._real = real
        self.calls = 0

    def evaluate(self, context):
        self.calls += 1
        return self._real.evaluate(context)


def _run_cycle(monkeypatch, *, scenario, missing=None, emergency=False,
               emergency_source=None, emergency_ts=None):
    """Run one real _apply_management cycle that actually produces `scenario`.

    scenario drives the REAL code path:
      - "STALE":        seed a future last-closed-candle ts (feed went backwards)
      - "INVALID":      the OHLCV fetch raises
      - "INSUFFICIENT": the frame has too few closed candles
      - "FRESH":        a normal fresh closed-candle frame
      - "UNCHANGED":    same closed candle as the last cycle
      - "MISSING":      skip the heavy calc and leave required indicators unset
    """
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frame = _frame(120, latest_closed_offset=-1200.0)
    last_closed_ms = int(frame["timestamp"].iloc[-1])
    last_closed_s = last_closed_ms / 1000.0

    if scenario == "INVALID":
        def boom(*a, **k):
            raise RuntimeError("BingX 503")
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", boom)
    elif scenario == "INSUFFICIENT":
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta",
                            _meta({"f": _frame(10, latest_closed_offset=-1200.0)}))
    elif scenario == "STALE":
        # Previous cycle saw a NEWER closed candle than the feed now reports.
        ggpbot.STATE["last_management_closed_candle_ts"] = last_closed_s + 900.0
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))
    elif scenario == "UNCHANGED":
        ggpbot.STATE["last_management_closed_candle_ts"] = last_closed_s
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))
    elif scenario == "MISSING":
        # Heavy calc skipped (within 5s window) -> indicators stay unset.
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))
        for key in ("adx_live", "di_plus_live", "di_minus_live",
                    "continuation_probability", "market_regime"):
            ggpbot.STATE[key] = None
    else:  # FRESH
        monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))

    if emergency:
        ggpbot.STATE["emergency_exit"] = True
        ggpbot.STATE["emergency_reason"] = "MANUAL_KILL_SWITCH"
        ggpbot.STATE["emergency_source"] = emergency_source if emergency_source is not None else "DASHBOARD"
        ggpbot.STATE["emergency_ts"] = emergency_ts if emergency_ts is not None else time.time()
    else:
        ggpbot.STATE["emergency_exit"] = False
        ggpbot.STATE["emergency_source"] = None
        ggpbot.STATE["emergency_ts"] = 0.0

    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    logs = []
    real = ggpbot.log_execution
    monkeypatch.setattr(ggpbot, "log_execution",
                        lambda m, lvl="INFO", *a, **k: (logs.append(str(m)), real(m, lvl, *a, **k))[1])

    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    spy = _BoardSpy(mgr.board)
    mgr.board = spy
    # MISSING: prevent the heavy calc from repopulating the indicators.
    mgr.last_heavy_calc_ts = time.time() if scenario == "MISSING" else 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    return mgr, spy, logs


def test_gate_blocks_board_on_stale(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE")
    assert spy.calls == 0, "STALE data must not reach board.evaluate"
    assert mgr.last_decision_blocked_reason == "stale_or_missing_market_data"
    assert any("DECISION_BLOCKED" in m for m in logs)


def test_gate_blocks_board_on_invalid(monkeypatch):
    # A fetch failure returns at the market-data error handler (before the gate),
    # which still guarantees board.evaluate is never reached.
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="INVALID")
    assert spy.calls == 0, "INVALID data must not reach board.evaluate"
    assert ggpbot.STATE["last_market_data_freshness"] == "INVALID"
    assert any("fetch error" in m for m in logs)


def test_gate_blocks_board_on_insufficient(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="INSUFFICIENT")
    assert spy.calls == 0, "INSUFFICIENT data must not reach board.evaluate"


def test_gate_blocks_board_when_required_indicators_missing(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="MISSING")
    assert spy.calls == 0, "missing ADX/DI/continuation must block the board"
    assert any("DECISION_BLOCKED" in m for m in logs)


def test_gate_allows_board_on_fresh(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="FRESH")
    assert spy.calls == 1, "FRESH data must allow a normal board decision"
    assert mgr.last_decision_blocked_reason is None


def test_gate_allows_board_on_unchanged_but_valid(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="UNCHANGED")
    assert spy.calls == 1, "UNCHANGED_BUT_VALID is valid and must not block"
    assert any("UNCHANGED_BUT_VALID" in m for m in logs)


def test_gate_stale_without_emergency_no_board_no_decision(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=False)
    assert spy.calls == 0, "STALE + no emergency -> board.evaluate must not run"
    assert mgr.last_decision_blocked_reason == "stale_or_missing_market_data"


def test_gate_stale_with_emergency_never_calls_board(monkeypatch):
    # A genuine emergency must NOT go through board.evaluate with stale indicators.
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=True)
    assert spy.calls == 0, "the emergency path must not call board.evaluate"


def test_emergency_rejected_without_source(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=True,
                                emergency_source="", emergency_ts=time.time())
    assert spy.calls == 0
    assert any("EMERGENCY_EXIT_REJECTED" in m and "no source" in m for m in logs)
    assert ggpbot.STATE["open"] is True


def test_emergency_rejected_when_signal_is_stale(monkeypatch):
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=True,
                                emergency_source="DASHBOARD", emergency_ts=time.time() - 999)
    assert spy.calls == 0
    assert any("EMERGENCY_EXIT_REJECTED" in m and "not fresh" in m for m in logs)
    assert ggpbot.STATE["open"] is True


def test_emergency_rejected_when_exchange_position_not_open(monkeypatch):
    monkeypatch.setattr(ggpbot, "fetch_position_tri_state", lambda *a, **k: ("FLAT", None))
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=True,
                                emergency_source="DASHBOARD", emergency_ts=time.time())
    assert spy.calls == 0
    assert any("EMERGENCY_EXIT_REJECTED" in m and "not confirmed OPEN" in m for m in logs)


def test_emergency_valid_executes_verified_close(monkeypatch):
    monkeypatch.setattr(ggpbot, "fetch_position_tri_state",
                        lambda *a, **k: ("OPEN", {"symbol": "SOL/USDT:USDT", "side": "long",
                                                  "contracts": 1.0, "entryPrice": 83.36}))
    monkeypatch.setattr(ggpbot, "safe_close_position",
                        lambda *a, **k: (ggpbot.STATE.update({"close_verified": True,
                                                              "last_close_filled_qty": 1.0}),
                                         True)[1])
    monkeypatch.setattr(ggpbot, "finalize_trade_with_reality", lambda *a, **k: None)
    mgr, spy, logs = _run_cycle(monkeypatch, scenario="STALE", emergency=True,
                                emergency_source="DASHBOARD", emergency_ts=time.time())
    assert spy.calls == 0, "verified emergency close must not use board.evaluate"
    assert any("EMERGENCY_EXIT_EXECUTED" in m for m in logs)
    assert ggpbot.STATE["emergency_exit"] is False, "signal must be cleared after execution"


def test_gate_block_does_not_change_protection(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    ggpbot.STATE.update({"synthetic_sl": 82.0, "tp1_price": 84.0, "tp2_price": 85.0,
                         "native_sl_order_id": "sl-1", "native_sl_verified": True})
    frame = _frame(120, latest_closed_offset=-1200.0)
    ggpbot.STATE["last_management_closed_candle_ts"] = frame["timestamp"].iloc[-1] / 1000.0 + 900.0
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.41)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)
    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    assert ggpbot.STATE["open"] is True
    assert ggpbot.STATE["synthetic_sl"] == 82.0
    assert ggpbot.STATE["tp1_price"] == 84.0
    assert ggpbot.STATE["tp2_price"] == 85.0
    assert ggpbot.STATE["native_sl_order_id"] == "sl-1"
    assert ggpbot.STATE["native_sl_verified"] is True


def test_gate_block_leaves_sync_and_monitoring_intact(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    frame = _frame(120, latest_closed_offset=-1200.0)
    ggpbot.STATE["last_management_closed_candle_ts"] = frame["timestamp"].iloc[-1] / 1000.0 + 900.0
    ggpbot.STATE["mark_price"] = 0.0
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe_meta", _meta({"f": frame}))
    monkeypatch.setattr(ggpbot, "get_ticker_safe", lambda *a, **k: 83.99)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)
    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr.last_heavy_calc_ts = 0.0
    mgr._apply_management("SOL/USDT:USDT", time.time())
    assert ggpbot.STATE["mark_price"] == 83.99, "mark price monitoring must still refresh"
    assert ggpbot.STATE["open"] is True


def test_force_close_event_is_independent_of_market_data(monkeypatch):
    # The force_close_local event path must still close a vanished position even
    # when market data is STALE — it is an independent safety path.
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    _seed_open_position(monkeypatch)
    ggpbot.STATE["last_market_data_freshness"] = "STALE"
    closed = {"n": 0}
    monkeypatch.setattr(ggpbot, "safe_close_position",
                        lambda *a, **k: (closed.__setitem__("n", closed["n"] + 1),
                                         ggpbot.STATE.update({"close_verified": True}),
                                         True)[2])
    monkeypatch.setattr(ggpbot, "finalize_trade_with_reality", lambda *a, **k: None)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)
    mgr = ggpbot.LiveTradeManager(ggpbot._event_bus, ggpbot._exchange_sync, ggpbot._recovery_guard)
    mgr.lifecycle_state = ggpbot.TradeLifecycleState.LIVE
    mgr._force_close({"reason": "POSITION_VANISHED"})
    assert closed["n"] == 1, "force_close must run regardless of market data freshness"
