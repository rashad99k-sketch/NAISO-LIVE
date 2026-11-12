"""Unit tests for bounded BingX API retry policy; no network or exchange credentials."""
import pytest

from bingx_api_reliability import ApiCooldownError, BingXApiReliability


class HttpError(Exception):
    def __init__(self, status_code, message="exchange error", headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = type("Response", (), {"status_code": status_code, "headers": headers or {}})()


def test_read_retries_one_transient_network_failure():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_positions(self):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("read timed out")
            return [{"symbol": "BTC/USDT:USDT"}]

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    result = policy.call_read(exchange.fetch_positions)
    assert result[0]["symbol"] == "BTC/USDT:USDT"
    assert exchange.calls == 2
    assert policy.snapshot()["read_retries"] == 1


def test_http_418_is_never_retried_and_opens_cooldown():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_balance(self):
            self.calls += 1
            raise HttpError(418, "HTTP 418 IP banned")

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    with pytest.raises(HttpError):
        policy.call_read(exchange.fetch_balance)
    assert exchange.calls == 1
    assert policy.snapshot()["banned"] == 1
    with pytest.raises(ApiCooldownError):
        policy.call_read(exchange.fetch_balance)
    assert exchange.calls == 1


def test_http_429_without_short_retry_after_is_not_retried():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_ticker(self):
            self.calls += 1
            raise HttpError(429, "HTTP 429 rate limit")

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    with pytest.raises(HttpError):
        policy.call_read(exchange.fetch_ticker)
    assert exchange.calls == 1
    assert policy.snapshot()["rate_limited"] == 1


def test_http_429_retries_once_only_when_retry_after_is_small():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_ticker(self):
            self.calls += 1
            if self.calls == 1:
                raise HttpError(429, "rate limit", {"Retry-After": "0.2"})
            return {"last": 100.0}

    exchange = Exchange()
    waits = []
    policy = BingXApiReliability(sleep=waits.append)
    assert policy.call_read(exchange.fetch_ticker) == {"last": 100.0}
    assert exchange.calls == 2
    assert waits == [0.2]


def test_unknown_and_write_methods_are_never_retried():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def create_order(self):
            self.calls += 1
            raise TimeoutError("write may have reached exchange")
        def custom_operation(self):
            self.calls += 1
            raise TimeoutError("unknown operation")

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    for method in (exchange.create_order, exchange.custom_operation):
        with pytest.raises(TimeoutError):
            policy.call_read(method)
    assert exchange.calls == 2


def test_fatal_auth_error_is_not_retried():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_balance(self):
            self.calls += 1
            raise RuntimeError("invalid API key")

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    with pytest.raises(RuntimeError):
        policy.call_read(exchange.fetch_balance)
    assert exchange.calls == 1
    assert policy.snapshot()["fatal_failures"] == 1
