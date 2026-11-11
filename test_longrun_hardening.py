from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parent
BOT = (ROOT / "ggpbot.py").read_text(encoding="utf-8")
RESOURCE = (ROOT / "roro_resource_manager.py").read_text(encoding="utf-8")

def test_exchange_sync_is_tristate_and_unknown_is_not_flat():
    assert 'status, pos = fetch_position_tri_state(symbol)' in BOT
    assert 'if status == "UNKNOWN":' in BOT
    assert 'if status == "FLAT":' in BOT
    assert 'exchange position state UNKNOWN' in BOT
    assert 'force_close_local' not in BOT[BOT.index('class ExchangeSyncService'):BOT.index('class RecoveryGuard')]

def test_reconciliation_has_independent_supervisor_and_lock():
    assert 'class ExchangeReconciliationSupervisor:' in BOT
    assert 'interval_sec=15.0' in BOT
    assert 'self._reconcile_lock = threading.Lock()' in BOT
    assert 'if not self._reconcile_lock.acquire(blocking=False):' in BOT

def test_external_flat_finalization_is_serialized():
    assert '_EXTERNAL_FLAT_FINALIZE_LOCK = threading.Lock()' in BOT
    assert 'def _clear_confirmed_flat_unlocked(' in BOT
    assert 'def _clear_confirmed_flat(symbol' in BOT

def test_runtime_logs_are_rotated_and_bounded():
    assert 'RotatingFileHandler' in BOT
    assert 'maxBytes=5 * 1024 * 1024, backupCount=10' in BOT
    assert 'len(DASHBOARD_STATE["logs"]) > 200' in BOT
    assert 'len(DASHBOARD_STATE["errors"]) > 50' in BOT

def test_resource_manager_bounds_cache_and_history():
    assert 'max_cache_items: int = 5000' in RESOURCE
    assert 'deque(maxlen=max(10, int(max_history)))' in RESOURCE
    assert 'while len(self.cache) > self.max_cache_items' in RESOURCE

def test_statistics_are_not_cleared_by_external_trade_runtime_cleanup():
    start = BOT.index('def _clear_confirmed_flat_unlocked')
    end = BOT.index('def _resolve_pending_close_from_snapshot', start)
    section = BOT[start:end]
    assert 'PERF["total_pnl_usdt"]' not in section or 'finalize_trade_with_reality' in section
    assert 'clear_position_dashboard()' in section
    assert 'STATE.update({"open": False' in section
