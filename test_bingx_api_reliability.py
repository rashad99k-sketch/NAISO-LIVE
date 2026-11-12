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
    policy = BingXApiReliability(endpoint_min_intervals={}, sleep=waits.append)
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


def test_public_market_data_endpoint_has_independent_one_per_second_pacing():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_order_book(self):
            self.calls += 1
            return {"bids": [], "asks": []}

    now = [0.0]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    exchange = Exchange()
    policy = BingXApiReliability(
        endpoint_min_intervals={"fetch_order_book": 1.05},
        clock=lambda: now[0],
        sleep=sleep,
    )
    policy.call_read(exchange.fetch_order_book)
    policy.call_read(exchange.fetch_order_book)

    assert exchange.calls == 2
    assert waits == [pytest.approx(1.05)]
    assert policy.snapshot()["endpoint_waits"] == 1


def test_bingx_109500_system_busy_is_not_retried_and_enters_cooldown():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_positions(self):
            self.calls += 1
            raise RuntimeError(
                'bingx {"code":109500,"msg":"The current system is busy, please try again later"}'
            )

    exchange = Exchange()
    policy = BingXApiReliability(
        system_busy_cooldown=20.0,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(RuntimeError):
        policy.call_read(exchange.fetch_positions)
    assert exchange.calls == 1
    assert policy.snapshot()["system_busy"] == 1
    with pytest.raises(ApiCooldownError):
        policy.call_read(exchange.fetch_positions)
    assert exchange.calls == 1


@pytest.mark.parametrize("code", [109403, 109429])
def test_bingx_risk_control_codes_open_cooldown_without_retry(code):
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_balance(self):
            self.calls += 1
            raise RuntimeError(f"bingx error code={code}")

    exchange = Exchange()
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    with pytest.raises(RuntimeError):
        policy.call_read(exchange.fetch_balance)
    assert exchange.calls == 1
    assert policy.snapshot()["risk_control"] == 1
    with pytest.raises(ApiCooldownError):
        policy.call_read(exchange.fetch_balance)
    assert exchange.calls == 1


def test_transient_read_retry_uses_bounded_jitter():
    class Exchange:
        def __init__(self):
            self.calls = 0
        def fetch_positions(self):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary network timeout")
            return []

    waits = []
    exchange = Exchange()
    policy = BingXApiReliability(
        transient_delay=0.25,
        retry_jitter_ratio=0.20,
        random_uniform=lambda low, high: high,
        sleep=waits.append,
    )

    assert policy.call_read(exchange.fetch_positions) == []
    assert exchange.calls == 2
    assert waits == [pytest.approx(0.30)]


def test_api_cooldown_blocks_new_entries_explicitly():
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    policy._set_cooldown(12.0, "HTTP_418_BAN")

    with pytest.raises(ApiCooldownError):
        policy.assert_new_entry_allowed()


@pytest.mark.parametrize("method_name", [
    "fetch_ticker", "fetch_ohlcv", "fetch_trades", "load_markets",
])
def test_other_documented_market_data_methods_are_endpoint_paced(method_name):
    class Exchange:
        def fetch_ticker(self):
            return {}
        def fetch_ohlcv(self):
            return []
        def fetch_trades(self):
            return []
        def load_markets(self):
            return {}

    now = [0.0]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    exchange = Exchange()
    policy = BingXApiReliability(clock=lambda: now[0], sleep=sleep)
    method = getattr(exchange, method_name)
    policy.call_read(method)
    policy.call_read(method)

    assert waits == [pytest.approx(1.05)]


def test_single_shot_write_rate_limit_sets_global_entry_cooldown():
    policy = BingXApiReliability(sleep=lambda _seconds: None)
    kind = policy.observe_error(RuntimeError("HTTP 429 Too Many Requests"), "create_order")
    assert kind == "RATE_LIMIT"
    with pytest.raises(ApiCooldownError):
        policy.assert_new_entry_allowed()


def test_expired_cooldown_requires_successful_read_probe_before_new_entry():
    now = [100.0]
    policy = BingXApiReliability(clock=lambda: now[0], sleep=lambda _seconds: None)
    policy._set_cooldown(5.0, "HTTP_418_BAN")

    class Exchange:
        def fetch_positions(self):
            return []

    now[0] += 6.0
    with pytest.raises(ApiCooldownError):
        policy.assert_new_entry_allowed()

    assert policy.call_read(Exchange().fetch_positions) == []
    policy.assert_new_entry_allowed()
