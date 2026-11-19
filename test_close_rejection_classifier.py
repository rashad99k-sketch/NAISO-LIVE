"""Regression tests for safe TP1 minimum-size fallback classification."""

import ggpbot


class InvalidOrder(Exception):
    pass


def test_bingx_minimum_notional_rejection_is_classified_for_tp1_fallback():
    exc = InvalidOrder("Order quantity is less than minimum notional value")
    assert ggpbot._is_min_amount_close_rejection(exc)


def test_bingx_minimum_trade_amount_rejection_is_classified():
    exc = InvalidOrder("Trade amount is below minimum")
    assert ggpbot._is_min_amount_close_rejection(exc)


def test_precision_and_position_mode_rejections_do_not_trigger_full_close_fallback():
    assert not ggpbot._is_min_amount_close_rejection(
        InvalidOrder("Invalid quantity precision")
    )
    assert not ggpbot._is_min_amount_close_rejection(
        InvalidOrder("Position side does not match position mode")
    )


def test_non_invalid_order_errors_never_trigger_minimum_size_fallback():
    assert not ggpbot._is_min_amount_close_rejection(
        RuntimeError("minimum notional")
    )
