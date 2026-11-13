import pytest

from roro_trade_journal import TradeJournal


def test_passive_wal_checkpoint_reports_status_and_preserves_runtime_state(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.sqlite3"))
    expected = {"expires_at": 1234567890, "reason": "drawdown_cooldown"}
    journal.set_runtime_state("loss_cooldown", expected)

    result = journal.checkpoint_wal("PASSIVE")

    assert result["mode"] == "PASSIVE"
    assert isinstance(result["busy"], int)
    assert isinstance(result["log_frames"], int)
    assert isinstance(result["checkpointed_frames"], int)
    assert journal.get_runtime_state("loss_cooldown") == expected


def test_wal_checkpoint_rejects_unknown_mode(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.sqlite3"))

    with pytest.raises(ValueError, match="Unsupported WAL checkpoint mode"):
        journal.checkpoint_wal("DELETE")
