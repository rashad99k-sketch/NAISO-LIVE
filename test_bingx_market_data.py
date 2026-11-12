import gzip
import json

import pytest

from bingx_market_data import BingXBookTickerFeed


class FakeSocket:
    def __init__(self):
        self.sent = []

    def send(self, value):
        self.sent.append(value)


def _message():
    return {
        "code": 0,
        "dataType": "BTC-USDT@bookTicker",
        "data": {
            "s": "BTC-USDT",
            "b": "100.0",
            "B": "2.5",
            "a": "100.1",
            "A": "3.0",
            "T": 1794500000000,
        },
    }


def test_bookticker_parses_gzip_and_normalizes_top_of_book():
    feed = BingXBookTickerFeed()
    socket = FakeSocket()

    feed._on_message(socket, gzip.compress(json.dumps(_message()).encode()))

    book = feed._books["BTC-USDT"]
    assert book["bid"] == 100.0
    assert book["ask"] == 100.1
    assert book["bid_qty"] == 2.5
    assert book["ask_qty"] == 3.0
    assert book["exchange_ts_ms"] == 1794500000000


def test_ping_receives_pong():
    feed = BingXBookTickerFeed()
    socket = FakeSocket()

    feed._on_message(socket, gzip.compress(b"Ping"))

    assert socket.sent == ["Pong"]


def test_stale_book_is_not_returned(monkeypatch):
    now = [10.0]
    feed = BingXBookTickerFeed(clock=lambda: now[0], stale_after_sec=1.0)
    monkeypatch.setattr(feed, "start", lambda: True)
    feed._on_message(FakeSocket(), json.dumps(_message()))

    assert feed.get("BTC/USDT:USDT") is not None
    now[0] += 1.1
    assert feed.get("BTC/USDT:USDT") is None


@pytest.mark.parametrize("symbol", ["", "BTC/USDT?foo=1", "BTC/USDT:BTC"])
def test_invalid_or_non_usdt_symbol_is_rejected(symbol):
    feed = BingXBookTickerFeed()
    with pytest.raises(ValueError):
        feed.subscribe(symbol)


def test_subscribe_uses_bingx_bookticker_channel():
    feed = BingXBookTickerFeed()
    socket = FakeSocket()
    with feed._lock:
        feed._connected = True
        feed._ws = socket
    monkeypatch_start = feed.start
    feed.start = lambda: True

    assert feed.subscribe("ETH/USDT:USDT") is True
    payload = json.loads(socket.sent[-1])
    assert payload["reqType"] == "sub"
    assert payload["dataType"] == "ETH-USDT@bookTicker"
    feed.start = monkeypatch_start


def test_repeated_gets_do_not_send_duplicate_subscriptions():
    feed = BingXBookTickerFeed()
    socket = FakeSocket()
    feed.start = lambda: True
    with feed._lock:
        feed._connected = True
        feed._ws = socket

    feed.subscribe("BTC/USDT:USDT")
    feed.subscribe("BTC/USDT:USDT")

    assert len(socket.sent) == 1


def test_depth_channel_and_snapshot_are_parsed_and_sorted():
    feed = BingXBookTickerFeed()
    feed.start = lambda: True
    socket = FakeSocket()
    with feed._lock:
        feed._connected = True
        feed._ws = socket

    message = {
        "code": 0,
        "dataType": "SOL-USDT@depth10@500ms",
        "ts": 1794500000000,
        "data": {
            "bids": [["99.0", "2"], ["100.0", "1"]],
            "asks": [["101.0", "3"], ["100.5", "4"]],
        },
    }
    feed._on_message(socket, gzip.compress(json.dumps(message).encode()))

    depth = feed.get_depth("SOL/USDT:USDT", 10)
    assert depth["bids"] == [[100.0, 1.0], [99.0, 2.0]]
    assert depth["asks"] == [[100.5, 4.0], [101.0, 3.0]]
    assert depth["server_ts_ms"] == 1794500000000
    assert json.loads(socket.sent[-1])["dataType"] == "SOL-USDT@depth10@500ms"


def test_depth_interval_uses_fast_interval_only_for_btc_and_eth():
    assert BingXBookTickerFeed._depth_channel("BTC/USDT:USDT", 10) == "BTC-USDT@depth10@200ms"
    assert BingXBookTickerFeed._depth_channel("ETH/USDT:USDT", 20) == "ETH-USDT@depth20@200ms"
    assert BingXBookTickerFeed._depth_channel("SOL/USDT:USDT", 10) == "SOL-USDT@depth10@500ms"


def test_disconnect_invalidates_old_book_and_depth_snapshots():
    feed = BingXBookTickerFeed()
    feed.start = lambda: True
    socket = FakeSocket()
    message = {
        "code": 0,
        "dataType": "BTC-USDT@depth10@200ms",
        "data": {
            "bids": [["100", "1"]],
            "asks": [["101", "1"]],
        },
    }
    feed._on_message(socket, json.dumps(message))
    assert feed._depths
    feed._on_close(socket, 1006, "disconnect")
    assert not feed._depths
    assert feed.get_depth("BTC/USDT:USDT", 10) is None


def test_depth_subscription_lru_cap_evicts_old_channel():
    feed = BingXBookTickerFeed(max_subscriptions=1, subscription_interval_sec=0.05)
    feed.start = lambda: True
    socket = FakeSocket()
    with feed._lock:
        feed._connected = True
        feed._ws = socket

    feed.subscribe_depth("BTC/USDT:USDT", 10)
    feed.subscribe_depth("ETH/USDT:USDT", 10)

    requests = [json.loads(item) for item in socket.sent]
    assert [item["reqType"] for item in requests] == ["sub", "unsub", "sub"]
    assert requests[1]["dataType"] == "BTC-USDT@depth10@200ms"
    assert requests[2]["dataType"] == "ETH-USDT@depth10@200ms"
    assert feed._desired == {"ETH-USDT@depth10@200ms"}


def test_depth_uses_its_own_local_freshness_window():
    now = [50.0]
    feed = BingXBookTickerFeed(
        clock=lambda: now[0],
        stale_after_sec=1.0,
        depth_stale_after_sec=3.0,
    )
    feed.start = lambda: True
    message = {
        "code": 0,
        "dataType": "SOL-USDT@depth10@500ms",
        "data": {
            "bids": [["100", "1"]],
            "asks": [["101", "1"]],
        },
    }
    feed._on_message(FakeSocket(), json.dumps(message))

    assert feed.get_depth("SOL/USDT:USDT", 10) is not None
    now[0] += 2.0
    assert feed.get_depth("SOL/USDT:USDT", 10) is not None
    now[0] += 1.1
    assert feed.get_depth("SOL/USDT:USDT", 10) is None
