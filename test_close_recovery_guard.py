import pytest

from ggp_close_recovery import (
    amount_step_from_market,
    ceil_amount_to_step,
    floor_amount_to_step,
    is_contract_amount_precision_error,
    fetch_order_by_client_id,
    recover_integer_contract_amount,
)


@pytest.mark.parametrize(
    "contracts,expected_close,expected_remaining",
    [
        (49, 24, 25),
        (25, 12, 13),
        (100, 50, 50),
        (7, 3, 4),
    ],
)
def test_integer_contract_tp1_never_overcloses_half(contracts, expected_close, expected_remaining):
    raw_half = contracts * 0.5
    close_qty = floor_amount_to_step(raw_half, 1)
    remaining_target = ceil_amount_to_step(raw_half, 1)
    assert close_qty == expected_close
    assert remaining_target == expected_remaining
    assert contracts - close_qty == expected_remaining


def test_decimal_step_preserves_supported_fractional_contracts():
    assert floor_amount_to_step(24.559, 0.01) == pytest.approx(24.55)
    assert ceil_amount_to_step(24.551, 0.01) == pytest.approx(24.56)


def test_sub_contract_partial_is_not_promoted_to_a_full_contract():
    assert floor_amount_to_step(0.5, 1) == 0.0


def test_market_step_uses_ccxt_precision_mode_without_guessing():
    market = {"contract": True, "precision": {"amount": 1}}
    assert amount_step_from_market(market, 4) == 1.0
    assert amount_step_from_market(market, 2) == 0.1
    assert amount_step_from_market(market, 3) is None
    assert amount_step_from_market({"contract": False, "precision": {"amount": 1}}, 4) is None


def test_only_exact_contract_precision_error_is_classified():
    assert is_contract_amount_precision_error(
        "bingx amount of XRP/USDT:USDT must be greater than minimum amount precision of 1"
    )
    assert not is_contract_amount_precision_error("Request timeout")
    assert not is_contract_amount_precision_error("insufficient margin")



@pytest.mark.parametrize(
    "requested,before,observed,status,expected",
    [
        (24.5, 49, 49, "OPEN", 24.0),
        (12.5, 25, 25, "OPEN", 12.0),
        (1.5, 3, 3, "OPEN", 1.0),
        (1.0, 3, 3, "OPEN", 0.0),
        (0.5, 1, 1, "OPEN", 0.0),
        (24.5, 49, 48, "OPEN", None),
        (24.5, 49, 49, "UNKNOWN", None),
        (24.5, 49, 0, "FLAT", None),
    ],
)
def test_retry_requires_unchanged_open_exposure(requested, before, observed, status, expected):
    assert recover_integer_contract_amount(requested, before, observed, status) == expected


def test_zero_decimal_places_means_whole_contracts():
    assert amount_step_from_market(
        {"contract": True, "precision": {"amount": 0}}, 2
    ) == 1.0



def test_client_id_lookup_uses_ccxt_params_contract():
    calls = []

    def fetch_order(order_id, symbol, params):
        calls.append((order_id, symbol, params))
        return {"id": "exchange-123", "status": "closed"}

    result = fetch_order_by_client_id(fetch_order, "XRP/USDT:USDT", "naiso-close-abc")
    assert result["id"] == "exchange-123"
    assert calls == [(
        "naiso-close-abc",
        "XRP/USDT:USDT",
        {"clientOrderId": "naiso-close-abc"},
    )]


def test_client_id_lookup_falls_back_for_two_argument_exchange_adapters():
    calls = []

    def fetch_order(order_id, symbol):
        calls.append((order_id, symbol))
        return {"id": "exchange-456"}

    result = fetch_order_by_client_id(fetch_order, "BTC/USDT:USDT", "naiso-close-def")
    assert result == {"id": "exchange-456"}
    assert calls == [("naiso-close-def", "BTC/USDT:USDT")]


def test_client_id_lookup_never_submits_or_raises_on_lookup_failure():
    def unavailable(*args, **kwargs):
        raise RuntimeError("temporary lookup failure")

    assert fetch_order_by_client_id(unavailable, "ETH/USDT:USDT", "naiso-close-ghi") is None
    assert fetch_order_by_client_id(None, "ETH/USDT:USDT", "naiso-close-ghi") is None
