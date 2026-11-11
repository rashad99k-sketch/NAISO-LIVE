import pytest
from ggp_position_sizing import calculate_position_size

@pytest.mark.parametrize("classification,allocation", [("TREND", 0.20), ("STRONG", 0.30)])
def test_risk_budget_is_ten_percent_of_target_margin(classification, allocation):
    r = calculate_position_size(
        classification=classification, available_balance=34.4755,
        entry_price=1.0, stop_price=0.99, side="BUY", leverage=20,
        max_trade_risk_pct=10.0, stop_execution_buffer_pct=0.0)
    assert r.target_margin == pytest.approx(34.4755 * allocation)
    assert r.max_risk_usdt == pytest.approx(r.target_margin * 0.10)
    assert r.estimated_loss_at_stop <= r.max_risk_usdt * 1.000001
    assert r.estimated_risk_pct <= 10.0 + 1e-8

def test_wide_stop_reduces_quantity_to_margin_risk_budget():
    r = calculate_position_size(
        classification="TREND", available_balance=34.4755,
        entry_price=1.0, stop_price=0.97, side="BUY", leverage=20,
        max_trade_risk_pct=10.0, stop_execution_buffer_pct=0.0)
    assert r.binding_constraint == "RISK_GATE"
    assert r.max_risk_usdt == pytest.approx(34.4755 * 0.20 * 0.10)
    assert r.estimated_loss_at_stop <= r.max_risk_usdt * 1.000001

def test_risk_budget_does_not_use_wallet_balance_directly():
    r = calculate_position_size(
        classification="TREND", available_balance=100.0,
        entry_price=1.0, stop_price=0.999, side="BUY", leverage=20,
        max_trade_risk_pct=10.0, stop_execution_buffer_pct=0.0)
    assert r.max_risk_usdt == pytest.approx(2.0)  # 10% of $20 target margin, not $100 wallet
