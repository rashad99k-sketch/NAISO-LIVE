import pandas as pd

import ggpbot


def test_spread_gate_prefers_fresh_bookticker_over_rest_depth(monkeypatch):
    class Feed:
        def get(self, symbol):
            assert symbol == "BTC/USDT:USDT"
            return {"bid": 100.0, "ask": 100.05}

    monkeypatch.setattr(ggpbot, "BINGX_MARKET_DATA", Feed())
    monkeypatch.setattr(
        ggpbot, "get_orderbook_cached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("REST depth must not be called")),
    )

    assert ggpbot.get_spread_bps("BTC/USDT:USDT") == 0.05


def test_dynamic_spread_tolerance_uses_validated_management_candle_count(monkeypatch):
    requested = []
    frame = pd.DataFrame({
        "open": [100.0] * 120,
        "high": [101.0] * 120,
        "low": [99.0] * 120,
        "close": [100.0] * 120,
        "volume": [1.0] * 120,
    })
    monkeypatch.setattr(
        ggpbot, "get_ohlcv_safe",
        lambda symbol, limit: requested.append((symbol, limit)) or frame,
    )
    monkeypatch.setattr(ggpbot, "compute_atr", lambda _frame: pd.Series([1.0] * 120))

    result = ggpbot.dynamic_spread_tolerance("BTC/USDT:USDT")

    assert requested == [("BTC/USDT:USDT", ggpbot.MANAGEMENT_OHLCV_LIMIT)]
    assert result == ggpbot.MAX_SPREAD_PERCENT_DEFAULT


def test_liquidity_depth_uses_websocket_snapshot_without_rest(monkeypatch):
    class Feed:
        def get_depth(self, symbol, level):
            assert symbol == "SOL/USDT:USDT"
            assert level == 10
            return {
                "bids": [[100.0, 2.0]],
                "asks": [[100.1, 3.0]],
                "server_ts_ms": 1794500000000,
                "received_mono": 10.0,
            }

    monkeypatch.setattr(ggpbot, "BINGX_MARKET_DATA", Feed())
    monkeypatch.setattr(ggpbot, "BINGX_REST_DEPTH_FALLBACK_ENABLED", False)
    monkeypatch.setattr(
        ggpbot, "fetch_orderbook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("REST depth must not be called")),
    )
    monkeypatch.setitem(ggpbot.STATE, "open", False)
    monkeypatch.setitem(ggpbot.TRADE_STATE, "in_position", False)

    result = ggpbot.get_orderbook_cached("SOL/USDT:USDT", 10)

    assert result["source"] == "BINGX_WS_DEPTH"
    assert result["bids"] == [[100.0, 2.0]]
    assert result["asks"] == [[100.1, 3.0]]


def test_scanner_does_not_fall_back_to_rest_depth_by_default(monkeypatch):
    class Feed:
        def get_depth(self, _symbol, _level):
            return None

    monkeypatch.setattr(ggpbot, "BINGX_MARKET_DATA", Feed())
    monkeypatch.setattr(ggpbot, "BINGX_REST_DEPTH_FALLBACK_ENABLED", False)
    monkeypatch.setattr(
        ggpbot, "fetch_orderbook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scanner REST fallback disabled")),
    )
    monkeypatch.setitem(ggpbot.STATE, "open", False)
    monkeypatch.setitem(ggpbot.TRADE_STATE, "in_position", False)

    assert ggpbot.get_orderbook_cached("ADA/USDT:USDT", 10) is None


def test_explicit_execution_fallback_can_use_rate_limited_rest_depth(monkeypatch):
    class Feed:
        def get_depth(self, _symbol, _level):
            return None

    book = {"bids": [[100.0, 1.0]], "asks": [[100.1, 1.0]]}
    monkeypatch.setattr(ggpbot, "BINGX_MARKET_DATA", Feed())
    monkeypatch.setattr(ggpbot, "fetch_orderbook", lambda *_args, **_kwargs: dict(book))
    monkeypatch.setitem(ggpbot.STATE, "open", False)
    monkeypatch.setitem(ggpbot.TRADE_STATE, "in_position", False)

    result = ggpbot.get_orderbook_cached("XRP/USDT:USDT", 5, rest_fallback=True)

    assert result["source"] == "BINGX_REST_DEPTH"
    assert result["bids"] == book["bids"]
