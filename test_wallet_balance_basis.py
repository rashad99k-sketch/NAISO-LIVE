import ggpbot
import threading
import time


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
    monkeypatch.setattr(ggpbot, "safe_api_call", lambda func, *args, **kwargs: func(*args, **kwargs))
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


def test_normal_strategy_sizing_basis_can_select_free_or_total(monkeypatch):
    snapshot = {"total": 33.0, "free": 7.5}
    monkeypatch.setattr(ggpbot, "POSITION_SIZING_BALANCE_BASIS", "AVAILABLE_FREE")
    free_value, free_basis = ggpbot.resolve_position_sizing_balance(snapshot)
    assert free_value == 7.5
    assert free_basis == "AVAILABLE_FREE"

    monkeypatch.setattr(ggpbot, "POSITION_SIZING_BALANCE_BASIS", "WALLET_TOTAL")
    total_value, total_basis = ggpbot.resolve_position_sizing_balance(snapshot)
    assert total_value == 33.0
    assert total_basis == "WALLET_TOTAL"


def test_concurrent_wallet_balance_snapshots_share_one_exchange_read(monkeypatch):
    class SlowBalanceExchange:
        def __init__(self):
            self.calls = 0
        def fetch_balance(self):
            self.calls += 1
            time.sleep(0.05)
            return {"total": {"USDT": 33.0}, "free": {"USDT": 7.5}}

    exchange = SlowBalanceExchange()
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "ex", exchange)
    monkeypatch.setattr(ggpbot, "safe_api_call", lambda func, *args, **kwargs: func(*args, **kwargs))
    ggpbot.CACHE.pop("wallet_balance_snapshot", None)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(ggpbot.get_wallet_balance_snapshot()))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert exchange.calls == 1
    assert [item["free"] for item in results] == [7.5, 7.5]
