import ast
from pathlib import Path

from roro_trade_journal import TradeJournal


def test_entry_signal_consumption_survives_journal_restart(tmp_path):
    path = tmp_path / "trade-journal.sqlite3"
    first = TradeJournal(str(path))
    assert first.consume_event("BUY:1795132800000", "trade-1") is True
    assert first.is_event_consumed("BUY:1795132800000") is True

    restarted = TradeJournal(str(path))
    assert restarted.is_event_consumed("BUY:1795132800000") is True
    assert restarted.consume_event("BUY:1795132800000", "trade-2") is False


def test_distinct_closed_candles_remain_independent_events(tmp_path):
    journal = TradeJournal(str(tmp_path / "trade-journal.sqlite3"))
    assert journal.consume_event("BUY:1795132800000", "trade-1") is True
    assert journal.consume_event("BUY:1795133700000", "trade-2") is True


def test_entry_checks_current_event_before_order_and_persists_after_verified_fill():
    source = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "execute_entry"]
    assert len(functions) >= 2
    core_entry, wrapper = functions[0], functions[-1]

    core_calls = [node.func.attr if isinstance(node.func, ast.Attribute)
                  else node.func.id if isinstance(node.func, ast.Name) else ""
                  for node in ast.walk(core_entry) if isinstance(node, ast.Call)]
    assert "is_event_consumed" in core_calls
    assert 'gate_result["durable_event_id"] = f"{event_symbol}:{gate_tf}:{raw_signal_id}"' in source
    guard_line = min(node.lineno for node in ast.walk(core_entry)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "is_event_consumed")
    sizing_line = min(node.lineno for node in ast.walk(core_entry)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "calculate_position_size")
    submit_line = min(node.lineno for node in ast.walk(core_entry)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "open_position")
    assert guard_line < sizing_line < submit_line

    wrapper_calls = [node.func.attr if isinstance(node.func, ast.Attribute)
                     else node.func.id if isinstance(node.func, ast.Name) else ""
                     for node in ast.walk(wrapper) if isinstance(node, ast.Call)]
    assert "_original_execute_entry_for_journal" in wrapper_calls
    assert "consume_event" in wrapper_calls
    assert any(isinstance(node, ast.With) for node in wrapper.body)

    original_call_line = min(
        node.lineno for node in ast.walk(wrapper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_original_execute_entry_for_journal"
    )
    consume_call_line = min(
        node.lineno for node in ast.walk(wrapper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "consume_event"
    )
    assert original_call_line < consume_call_line
