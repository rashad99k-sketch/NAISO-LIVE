from roro_trade_journal import TradeJournal


def test_runtime_state_round_trips_across_journal_instances(tmp_path):
    db_path = tmp_path / "trade-journal.sqlite3"
    first = TradeJournal(str(db_path))
    payload = {
        "until": 1_800_000_600.0,
        "triggered_at": 1_800_000_000.0,
        "trigger_balance": 95.0,
        "utc_day": "2026-11-13",
        "peak_balance": 100.0,
        "trigger_count": 1,
    }

    first.set_runtime_state("daily_drawdown_cooldown_v1", payload)

    restarted = TradeJournal(str(db_path))
    assert restarted.get_runtime_state("daily_drawdown_cooldown_v1") == payload


def test_runtime_state_is_independent_of_trade_rows(tmp_path):
    journal = TradeJournal(str(tmp_path / "trade-journal.sqlite3"))
    journal.set_runtime_state("daily_drawdown_cooldown_v1", {"until": 123.0})

    assert list(journal.open_trades()) == []
    assert journal.get_runtime_state("daily_drawdown_cooldown_v1") == {"until": 123.0}


def test_missing_runtime_state_returns_default(tmp_path):
    journal = TradeJournal(str(tmp_path / "trade-journal.sqlite3"))
    assert journal.get_runtime_state("not-present", {"until": 0.0}) == {"until": 0.0}
