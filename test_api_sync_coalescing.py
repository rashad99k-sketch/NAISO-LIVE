"""Regression tests for coalesced BingX position/PnL reads and overload handling."""

import os

import pytest

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("BINGX_API_KEY", "")
os.environ.setdefault("BINGX_API_SECRET", "")

import ggpbot


@pytest.fixture(autouse=True)
def _reset_api_caches(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "safe_api_call", lambda func, *args, **kwargs: func(*args, **kwargs))
    with ggpbot._POSITION_FETCH_CACHE_LOCK:
        ggpbot._POSITION_FETCH_CACHE.clear()
    with ggpbot._REALIZED_PNL_CACHE_LOCK:
        ggpbot._REALIZED_PNL_CACHE.clear()
    yield
    with ggpbot._POSITION_FETCH_CACHE_LOCK:
        ggpbot._POSITION_FETCH_CACHE.clear()
    with ggpbot._REALIZED_PNL_CACHE_LOCK:
        ggpbot._REALIZED_PNL_CACHE.clear()


class _PositionExchange:
    def __init__(self, position=None, error=None):
        self.position = position
        self.error = error
        self.calls = 0

    def fetch_positions(self, symbols=None):
        self.calls += 1
        if self.error:
            raise self.error
        return [self.position] if self.position else []


class _TradeExchange:
    def __init__(self, trades=None, error=None):
        self.trades = list(trades or [])
        self.error = error
        self.calls = 0

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.trades)


def _open_position():
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "contracts": 1.25,
        "entryPrice": 100.0,
        "markPrice": 101.0,
        "unrealizedPnl": 1.25,
        "initialMargin": 6.25,
        "leverage": 20.0,
        "info": {"positionSide": "LONG", "positionAmt": "1.25"},
    }


def test_background_position_reads_share_cache_but_force_read_bypasses_it(monkeypatch):
    exchange = _PositionExchange(position=_open_position())
    monkeypatch.setattr(ggpbot, "ex", exchange)

    first_status, first = ggpbot.fetch_position_tri_state("BTC/USDT:USDT", force=False)
    second_status, second = ggpbot.fetch_position_tri_state("BTC/USDT:USDT", force=False)

    assert first_status == second_status == "OPEN"
    assert first is second
    assert exchange.calls == 1

    # The default remains force=True for execution-critical paths such as
    # close submission and post-close position-delta verification.
    fresh_status, _ = ggpbot.fetch_position_tri_state("BTC/USDT:USDT")
    assert fresh_status == "OPEN"
    assert exchange.calls == 2


def test_unknown_position_result_is_briefly_coalesced_not_treated_as_flat(monkeypatch):
    exchange = _PositionExchange(error=RuntimeError(
        'bingx {"code":109500,"msg":"The current system is busy, please try again later"}'
    ))
    monkeypatch.setattr(ggpbot, "ex", exchange)

    first_status, first_position = ggpbot.fetch_position_tri_state("SAND/USDT:USDT", force=False)
    second_status, second_position = ggpbot.fetch_position_tri_state("SAND/USDT:USDT", force=False)

    assert first_status == second_status == "UNKNOWN"
    assert first_position is second_position is None
    assert exchange.calls == 1


def test_realized_pnl_reads_are_cached_and_force_refresh_is_supported(monkeypatch):
    exchange = _TradeExchange(trades=[
        {"side": "buy", "amount": 1.0, "price": 100.0},
        {"side": "sell", "amount": 1.0, "price": 105.0},
    ])
    monkeypatch.setattr(ggpbot, "ex", exchange)
    monkeypatch.setattr(ggpbot, "get_balance_safe", lambda: 100.0)

    first = ggpbot.get_realized_pnl("BTC/USDT:USDT")
    second = ggpbot.get_realized_pnl("BTC/USDT:USDT")

    assert first == second == (5.0, 5.0)
    assert exchange.calls == 1

    refreshed = ggpbot.get_realized_pnl("BTC/USDT:USDT", force=True)
    assert refreshed == (5.0, 5.0)
    assert exchange.calls == 2


def test_verified_close_invalidation_forces_next_pnl_refresh(monkeypatch):
    exchange = _TradeExchange(trades=[
        {"side": "buy", "amount": 1.0, "price": 100.0},
        {"side": "sell", "amount": 1.0, "price": 105.0},
    ])
    monkeypatch.setattr(ggpbot, "ex", exchange)
    monkeypatch.setattr(ggpbot, "get_balance_safe", lambda: 100.0)

    ggpbot.get_realized_pnl("BTC/USDT:USDT")
    assert exchange.calls == 1

    ggpbot._invalidate_realized_pnl_cache("BTC/USDT:USDT")
    ggpbot.get_realized_pnl("BTC/USDT:USDT")

    assert exchange.calls == 2


def test_realized_pnl_busy_error_uses_backoff_and_preserves_last_value(monkeypatch):
    exchange = _TradeExchange(error=RuntimeError(
        'bingx {"code":109500,"msg":"The current system is busy, please try again later"}'
    ))
    monkeypatch.setattr(ggpbot, "ex", exchange)

    first = ggpbot.get_realized_pnl("SAND/USDT:USDT")
    second = ggpbot.get_realized_pnl("SAND/USDT:USDT")

    assert first == second == (0.0, 0.0)
    assert exchange.calls == 1
    assert ggpbot.MEMORY["realized_pnl_sync"]["status"] == "UNAVAILABLE_BACKOFF"


def test_safe_api_call_does_not_burst_retry_bingx_system_busy(monkeypatch):
    calls = {"count": 0}

    def busy_read():
        calls["count"] += 1
        raise RuntimeError(
            'bingx {"code":109500,"msg":"The current system is busy, please try again later"}'
        )

    monkeypatch.setattr(ggpbot, "rate_limit", lambda: None)
    with pytest.raises(RuntimeError, match="109500"):
        ggpbot.safe_api_call(busy_read)

    assert calls["count"] == 1
