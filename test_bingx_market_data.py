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
