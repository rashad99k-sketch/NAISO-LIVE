"""Opt-in PostgreSQL journal contract tests.

Set TEST_POSTGRES_DSN to a disposable PostgreSQL database. Never point this
suite at production or a database containing live trading records.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"),
    reason="requires a disposable TEST_POSTGRES_DSN; never use production DATABASE_URL",
)


@pytest.fixture()
def journal():
    from roro_postgres_trade_journal import PostgresTradeJournal
    # Test DB must be disposable. Unique trade IDs avoid collisions between runs.
    instance = PostgresTradeJournal(
        os.environ["TEST_POSTGRES_DSN"],
        connect_timeout=5,
        statement_timeout_ms=5000,
    )
    yield instance


def test_postgres_health_and_schema(journal):
    result = journal.health_check()
    assert result["ok"] is True
    assert result["schema_version"] >= 1


def test_entry_snapshot_event_and_restart_reads(journal):
    trade_id = "contract-" + uuid.uuid4().hex
    client_id = "client-" + uuid.uuid4().hex
    journal.persist_entry_pending(
        trade_id=trade_id, client_order_id=client_id, symbol="BTC/USDT:USDT",
        side="BUY", position_mode="ONE_WAY", position_side="BOTH",
        leverage=20, requested_qty=0.01, entry_ts=1700000000.0,
        structural_stop=90000.0,
        management_state={"trade_id": trade_id, "tp1_price": 101000.0,
                          "tp2_price": 102000.0, "native_sl_original_price": 90000.0},
    )
    journal.record_execution(
        trade_id, exchange_order_id="fake-order-" + uuid.uuid4().hex,
        filled_qty=0.01, remaining_qty=0.0, average_fill_price=100000.0,
        actual_entry_price=100000.0, status="FILLED",
    )
    journal.start_trade(trade_id, "BTC/USDT:USDT", "BUY", "CONTRACT", 0.01, 100000.0, 90000.0)
    state = {
        "open": True, "status": "OPEN", "trade_lifecycle_state": "LIVE",
        "side": "BUY", "entry": 100000.0, "qty": 0.01, "initial_qty": 0.01,
        "remaining_qty": 0.01, "actual_filled_qty": 0.01,
        "native_sl_original_price": 90000.0, "native_sl_verified": True,
        "native_sl_status": "VERIFIED", "tp1_price": 101000.0, "tp2_price": 102000.0,
        "tp1_hit": False, "tp2_hit": False, "exchange_state": "OPEN",
    }
    assert journal.save_snapshot(trade_id, state, "snapshot-1", "CONTRACT_SNAPSHOT") is True
    assert journal.save_snapshot(trade_id, state, "snapshot-1", "CONTRACT_SNAPSHOT") is False
    restored = journal.get(trade_id)
    assert restored["state"]["status"] == "OPEN"
    assert restored["state"]["tp2_price"] == 102000.0
    rows = [row for row in journal.open_trades() if row["trade_id"] == trade_id]
    assert len(rows) == 1
    assert rows[0]["native_sl_verified"] is True
    assert journal.finalize_once(trade_id, "CONTRACT_TEST", 100100.0, 0.001) is True
    assert journal.finalize_once(trade_id, "CONTRACT_TEST", 100100.0, 0.001) is False
    assert journal.get(trade_id)["finalized"] is True


def test_consumed_event_is_idempotent(journal):
    trade_id = "event-" + uuid.uuid4().hex
    journal.start_trade(trade_id, "BTC/USDT:USDT", "BUY", "CONTRACT", 0.01, 100000.0)
    event_id = "evt-" + uuid.uuid4().hex
    assert journal.consume_event(event_id, trade_id) is True
    assert journal.consume_event(event_id, trade_id) is False
    assert journal.is_event_consumed(event_id) is True


def test_unknown_trade_snapshot_fails_closed(journal):
    with pytest.raises(RuntimeError, match="unknown trade_id"):
        journal.save_snapshot("missing-" + uuid.uuid4().hex, {"status": "OPEN"}, "missing-snapshot")
