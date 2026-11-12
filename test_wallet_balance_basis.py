import ggpbot


class FakeBalanceExchange:
    def __init__(self):
        self.calls = 0

    def fetch_balance(self):
        self.calls += 1
        return {
            "total": {"USDT": 33.0},
            "free": {"USDT": 7.5},
        }


def test_total_and_free_balance_are_read_from_one_exchange_snapshot(monkeypatch):
    exchange = FakeBalanceExchange()
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "ex", exchange)
    ggpbot.CACHE.clear()

    snapshot = ggpbot.get_wallet_balance_snapshot(max_age_sec=15.0)
    available, source, timestamp = ggpbot.get_available_balance_snapshot(max_age_sec=15.0)

    assert snapshot["total"] == 33.0
    assert snapshot["free"] == 7.5
    assert available == 7.5
    assert source == "BINGX_FUTURES_FREE_USDT"
    assert timestamp == snapshot["timestamp"]
    assert exchange.calls == 1


def test_paper_balance_snapshot_keeps_total_and_free_equal(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", True)
    monkeypatch.setitem(ggpbot.paper, "balance", 33.0)

    snapshot = ggpbot.get_wallet_balance_snapshot()

    assert snapshot["total"] == 33.0
    assert snapshot["free"] == 33.0
    assert snapshot["source"] == "PAPER"
