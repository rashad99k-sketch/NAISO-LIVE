import math

from ggp_profit_brain import (
    RANGE_PROFIT_TARGETS_PCT,
    TREND_TP1_MOVE_PCT,
    TREND_TP2_MOVE_PCT,
    trend_target_prices,
)


def test_range_targets_are_requested_price_moves():
    assert RANGE_PROFIT_TARGETS_PCT == (0.25, 0.50, 1.00)


def test_trend_targets_are_one_and_three_percent_for_long():
    tp1, tp2 = trend_target_prices("BUY", 100.0)
    assert math.isclose(tp1, 102.0)
    assert math.isclose(tp2, 103.0)


def test_trend_targets_are_one_and_three_percent_for_short():
    tp1, tp2 = trend_target_prices("SELL", 100.0)
    assert math.isclose(tp1, 98.0)
    assert math.isclose(tp2, 97.0)


def test_trend_target_configuration_is_explicit():
    assert TREND_TP1_MOVE_PCT == 2.0
    assert TREND_TP2_MOVE_PCT == 3.0


def test_invalid_side_or_entry_fails_closed():
    assert trend_target_prices("HOLD", 100.0) == (0.0, 0.0)
    assert trend_target_prices("BUY", 0.0) == (0.0, 0.0)
