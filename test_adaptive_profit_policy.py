from ggp_profit_brain import adaptive_range_target_price


def test_buy_targets_nearest_opposing_swing_before_pool():
    target, reason = adaptive_range_target_price("BUY", 100.0, 2.0, [100.5, 102.0, 104.0])
    assert reason == "nearest_opposing_swing_liquidity_pool"
    assert 100.0 < target < 102.0


def test_sell_targets_nearest_opposing_swing_before_pool():
    target, reason = adaptive_range_target_price("SELL", 100.0, 2.0, [99.0, 98.0, 96.0])
    assert reason == "nearest_opposing_swing_liquidity_pool"
    assert 98.0 < target < 100.0


def test_range_target_uses_atr_fallback_when_no_credible_pool():
    target, reason = adaptive_range_target_price("BUY", 100.0, 1.0, [100.1, 105.0])
    assert reason == "atr_fallback_no_credible_nearby_liquidity"
    assert target == 101.1


def test_invalid_target_inputs_fail_closed():
    assert adaptive_range_target_price("BUY", 0, 1, [2])[0] == 0
    assert adaptive_range_target_price("SIDEWAYS", 100, 1, [102])[0] == 0


def test_management_source_never_calls_native_stop_move():
    import ast
    from pathlib import Path
    source = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "move_native_stop_on_exchange"]
    assert calls == []
