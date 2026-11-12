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
