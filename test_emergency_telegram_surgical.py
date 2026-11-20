"""Static regression checks for emergency-first management and Telegram PnL reporting."""
import ast
from pathlib import Path

SOURCE = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _method(class_name, method_name):
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)


def _call_names(node):
    return [n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]


def test_apply_management_checks_emergency_before_market_data():
    method = _method("LiveTradeManager", "_apply_management")
    source = ast.get_source_segment(SOURCE, method)
    assert source.index("_emergency_exit_if_valid") < source.index("get_ticker_safe")
    assert source.index("_emergency_exit_if_valid") < source.index("get_ohlcv_safe_meta")


def test_manage_live_trade_checks_emergency_before_recovery_gates():
    method = _method("LiveTradeManager", "manage_live_trade")
    source = ast.get_source_segment(SOURCE, method)
    assert source.index("_emergency_exit_if_valid") < source.index('STATE.get("execution_unresolved")')
    assert source.index("_emergency_exit_if_valid") < source.index('STATE.get("native_sl_verified")')


def test_tp_notifications_use_exchange_realized_pnl_and_order_source():
    assert "realized_pnl_usdt=None" in SOURCE
    assert "Realized PnL: {safe_float(realized_pnl_usdt):+.4f} USDT" in SOURCE
    assert "tp1_realized = STATE.get(\"last_close_order_realized_pnl\")" in SOURCE
    assert "tp2_realized = STATE.get(\"last_close_order_realized_pnl\")" in SOURCE


def test_final_close_reports_total_realized_pnl_and_native_stop_details():
    assert "pnl_usdt=pnl_usdt if pnl_known else None" in SOURCE
    assert "aggregate_realized = prior_realized + safe_float(realized, 0.0)" in SOURCE
    assert "tg_sl_hit(symbol, roe_pct, pnl_usdt=total_realized" in SOURCE

def test_telegram_entry_report_includes_both_targets_and_both_entry_paths():
    assert 'f"🥇 TP1: {safe_float(tp1):.8f}"' in SOURCE
    assert 'f"🥈 TP2: {safe_float(tp2):.8f}"' in SOURCE
    assert SOURCE.count("tp2=tp2, qty=qty, leverage=LEVERAGE, trade_id=trade_id") == 2

def test_telegram_final_close_classifies_outcome_and_deduplicates_by_trade():
    assert 'outcome = "RESULT: PROFIT"' in SOURCE
    assert 'outcome = "RESULT: LOSS"' in SOURCE
    assert 'outcome = "RESULT: BREAKEVEN"' in SOURCE
    assert 'outcome = "RESULT: PNL UNAVAILABLE — NOT CLASSIFIED"' in SOURCE
    assert "total_qty=initial_qty, trade_id=trade_id" in SOURCE
    assert 'close_key = str(trade_id or (str(symbol) + "_" + reason))' in SOURCE

def test_telegram_health_exposes_delivery_counters_without_credentials():
    assert '"telegram": TELEGRAM_NOTIFIER.status()' in SOURCE
