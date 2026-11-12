"""End-to-end recovery, immutable-native-protection and capital-policy scenarios.

These drive the real GGP code paths (startup recovery, runtime recovery, the
verified market close, the fixed native-stop policy and the sizing engine)
against a deterministic mock exchange. No network or live keys are used.
"""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("BINGX_API_KEY", "")
os.environ.setdefault("BINGX_API_SECRET", "")

import ggpbot
from bingx_execution import (
    BingXPositionModeAdapter, ExecutionState, PositionMode, TradeExecutionService,
)
from ggp_position_sizing import (
    allocation_for_classification, calculate_position_size, effective_sizing_classification,
    is_strong_candidate,
)
from ggp_recovery import build_restored_management_state, reconcile_entry_fill, resolve_position_identity
from roro_trade_journal import TradeJournal


# --------------------------------------------------------------------------- #
# Mock exchange
# --------------------------------------------------------------------------- #
class MockExchange:
    """Minimal CCXT-BingX-shaped exchange with switchable behaviour."""

    def __init__(self, *, hedged=True, position=None, open_orders=None, entry_order=None,
                 balance=1000.0, cancel_replace=True, fail_cancel=False, close_reduces_to=None):
        self.hedged = hedged
        self.position = position
        self.open_orders = list(open_orders or [])
        self.entry_order = entry_order
        self.balance = balance
        self.cancel_replace = cancel_replace
        # Model "endpoint not available" the way a real exchange does: the method
        # is absent, so getattr(...) returns None rather than a callable that raises.
        if not cancel_replace:
            self.swapV1PrivatePostTradeCancelReplace = None
        self.fail_cancel = fail_cancel
        self.close_reduces_to = close_reduces_to
        self.create_calls = []
        self.cancel_calls = []
        self.cancel_replace_calls = []
        self._markets = {
            "BTC/USDT:USDT": {"id": "BTC-USDT", "contractSize": 1.0,
                              "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}},
            "DOT/USDT:USDT": {"id": "DOT-USDT", "contractSize": 1.0,
                              "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}},
        }

    # market data -----------------------------------------------------------
    def load_markets(self):
        return self._markets

    def market(self, symbol):
        return self._markets.get(symbol) or {
            "id": symbol.split("/")[0] + "-USDT", "contractSize": 1.0,
            "limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}}}

    def amount_to_precision(self, symbol, amount):
        return float(amount)

    def fetch_balance(self, params=None):
        return {"free": {"USDT": self.balance}}

    def fetch_ticker(self, symbol, params=None):
        # Mark price used by the native-stop PRECHECK directional sanity.
        mark = None
        if isinstance(self.position, dict):
            mark = self.position.get("markPrice")
        return {"last": mark, "close": mark}

    # position / mode -------------------------------------------------------
    def fetch_position_mode(self, symbol=None):
        return {"hedged": self.hedged}

    def fetch_positions(self, symbols=None):
        return [self.position] if self.position else []

    # orders ----------------------------------------------------------------
    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return list(self.open_orders)

    def swapV2PrivateGetTradeOpenOrders(self, params=None):
        return {"code": 0, "data": {"orders": []}}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.create_calls.append((symbol, kind, side, amount, params or {}))
        if kind == "STOP_MARKET":
            order_id = f"sl-{len(self.create_calls)}"
            self.open_orders.append({
                "id": order_id, "status": "open", "symbol": symbol, "type": "STOP_MARKET",
                "side": side, "positionSide": (params or {}).get("positionSide", "BOTH"),
                "stopPrice": (params or {}).get("stopPrice"), "quantity": amount,
            })
            return {"id": order_id, "status": "open"}
        return {"id": "mk-1", "status": "closed", "filled": amount, "remaining": 0.0,
                "average": 100.0}

    def cancel_order(self, order_id, symbol=None, params=None):
        self.cancel_calls.append((str(order_id), symbol))
        if self.fail_cancel:
            raise RuntimeError("cancel rejected")
        self.open_orders = [o for o in self.open_orders if str(o.get("id")) != str(order_id)]
        return {"id": order_id, "status": "canceled"}

    def swapV1PrivatePostTradeCancelReplace(self, params=None):
        if not self.cancel_replace:
            raise AttributeError("cancelReplace not supported by this account")
        self.cancel_replace_calls.append(params)
        old = str((params or {}).get("cancelOrderId"))
        self.open_orders = [o for o in self.open_orders if str(o.get("id")) != old]
        new_id = "sl-replaced"
        self.open_orders.append({
            "id": new_id, "status": "open", "symbol": "BTC/USDT:USDT", "type": "STOP_MARKET",
            "side": (params or {}).get("side", "SELL").lower(),
            "positionSide": (params or {}).get("positionSide", "BOTH"),
            "stopPrice": (params or {}).get("stopPrice"),
            "quantity": (params or {}).get("quantity"),
        })
        return {"code": 0, "data": {"replaceResult": "true", "newOrderResponse": {"orderId": new_id}}}

    def fetch_order(self, order_id, symbol=None, params=None):
        if self.entry_order and str(order_id) in {str(self.entry_order.get("id")),
                                                  str(self.entry_order.get("clientOrderId"))}:
            return self.entry_order
        return None

    def fetch_closed_orders(self, symbol=None, since=None, limit=None):
        return []

    def fetch_orders(self, symbol=None, since=None, limit=None):
        return []

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        return []


def _long_position(qty=2.0, entry=100.0, mark=101.0):
    return {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": qty, "entryPrice": entry,
            "markPrice": mark, "unrealizedPnl": (mark - entry) * qty, "initialMargin": entry * qty / 20,
            "leverage": 20.0, "info": {"positionSide": "LONG", "positionAmt": str(qty),
                                       "avgPrice": str(entry)}}


def _short_position(qty=2.0, entry=100.0, mark=99.0):
    return {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": qty, "entryPrice": entry,
            "markPrice": mark, "unrealizedPnl": (entry - mark) * qty, "initialMargin": entry * qty / 20,
            "leverage": 20.0, "info": {"positionSide": "SHORT", "positionAmt": str(-qty),
                                       "avgPrice": str(entry)}}


def _native_stop(qty=2.0, trigger=95.0, position_side="LONG", side="SELL", order_id="111"):
    return {"id": order_id, "status": "NEW", "symbol": "BTC-USDT", "type": "STOP_MARKET",
            "side": side, "positionSide": position_side, "stopPrice": str(trigger), "origQty": str(qty)}


def _fresh_state():
    """Reset the live STATE dict to its defaults for an isolated scenario."""
    template = dict(ggpbot.STATE)
    ggpbot.STATE.clear()
    for key in template:
        ggpbot.STATE[key] = None
    for flag in ("open", "recovery_hold", "execution_unresolved", "close_reconciliation_required",
                 "close_verified", "native_sl_verified", "tp1_hit", "tp2_hit", "be_done",
                 "trail_activated", "partial_closed"):
        ggpbot.STATE[flag] = False
    ggpbot.RECOVERY_BLOCK_ENTRIES = True


def _seed_journal(tmp_path, *, side="BUY", position_mode="HEDGE", position_side="LONG",
                  filled=2.0, entry=100.0, sl=95.0, tp1=105.0, tp2=110.0,
                  snapshot=True, snapshot_overrides=None, pending_state=None):
    journal = TradeJournal(str(Path(tmp_path) / "trade.sqlite3"))
    trade_id = "T-SCEN"
    journal.persist_entry_pending(
        trade_id=trade_id, client_order_id="GGP-CLIENT", symbol="BTC/USDT:USDT", side=side,
        position_mode=position_mode, position_side=position_side, leverage=20, requested_qty=filled,
        entry_ts=time.time(), status="ENTRY_PENDING", structural_stop=sl,
        management_state={"trade_id": trade_id, "synthetic_sl": sl, "sl": sl,
                          "native_sl_original_price": sl, "tp1_price": tp1, "tp2_price": tp2,
                          "initial_qty": filled, "qty": filled, "remaining_qty": filled})
    if pending_state is None:
        journal.record_execution(trade_id, exchange_order_id="E-1", filled_qty=filled,
                                 remaining_qty=0.0, average_fill_price=entry,
                                 actual_entry_price=entry, status="FILLED")
    journal.start_trade(trade_id, "BTC/USDT:USDT", side, "TREND", filled, entry, sl)
    if snapshot:
        state = {
            "open": True, "side": side, "entry": entry, "qty": filled, "initial_qty": filled,
            "exchange_qty": filled, "remaining_qty": filled, "sl": sl,
            "current_symbol": "BTC/USDT:USDT", "tp1_price": tp1, "tp2_price": tp2,
            "synthetic_sl": sl, "synthetic_tp1": tp1, "native_sl_original_price": sl,
            "trade_id": trade_id, "position_mode": position_mode, "position_side": position_side,
            "native_sl_order_id": "111", "native_sl_verified": True, "native_sl_status": "VERIFIED",
            "recovery_hold": False, "tp1_hit": False, "tp2_hit": False, "be_done": False,
            "trail_activated": False, "trail_stop": 0.0, "max_price": entry, "min_price": entry,
            "entry_time": time.time(), "trade_lifecycle_state": "LIVE",
        }
        if snapshot_overrides:
            state.update(snapshot_overrides)
        journal.save_snapshot(trade_id, state, event_key="entry-state:1", event_type="ENTRY_STATE")
    return journal, trade_id


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    """Force LIVE code paths with a mock exchange for every scenario."""
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", True)
    yield


# --------------------------------------------------------------------------- #
# 1 & 2: restart with a live position (complete + incomplete management data)
# --------------------------------------------------------------------------- #
def test_restart_live_position_with_complete_state_restores(tmp_path, monkeypatch):
    journal, _ = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(),
                                                   open_orders=[_native_stop()]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["recovery_hold"] is False
    assert ggpbot.STATE["native_sl_status"] == "VERIFIED"
    assert ggpbot.STATE["actual_filled_qty"] == 2.0
    assert ggpbot.RECOVERY_BLOCK_ENTRIES is False


def test_restart_live_position_with_incomplete_state_holds(tmp_path, monkeypatch):
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={"tp2_price": 0.0})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(),
                                                   open_orders=[_native_stop()]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["recovery_hold"] is True
    assert "persisted_tp2" in str(ggpbot.STATE["recovery_reason"])
    assert ggpbot.RECOVERY_BLOCK_ENTRIES is True


# --------------------------------------------------------------------------- #
# 3 & 5: missing native SL and symbol-format tolerance
# --------------------------------------------------------------------------- #
def test_missing_native_sl_keeps_recovery_hold(tmp_path, monkeypatch):
    journal, _ = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(), open_orders=[]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["recovery_hold"] is True
    assert "native_sl_missing" in str(ggpbot.STATE["recovery_reason"])


def test_native_sl_verifies_across_symbol_format_difference(tmp_path, monkeypatch):
    # Journal uses CCXT "BTC/USDT:USDT"; BingX open order uses raw "BTC-USDT".
    journal, _ = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(),
                                                   open_orders=[_native_stop()]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["native_sl_status"] == "VERIFIED"


# --------------------------------------------------------------------------- #
# 4: protection read failure must not be treated as verified
# --------------------------------------------------------------------------- #
def test_protection_read_failure_is_not_verified(tmp_path, monkeypatch):
    class BrokenRead(MockExchange):
        def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
            raise RuntimeError("network down")

        def swapV2PrivateGetTradeOpenOrders(self, params=None):
            raise RuntimeError("network down")

    journal, _ = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", BrokenRead(position=_long_position()))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["native_sl_verified"] is False
    assert ggpbot.STATE["recovery_hold"] is True


# --------------------------------------------------------------------------- #
# 6 & 7: Hedge/One-Way LONG and SHORT identity
# --------------------------------------------------------------------------- #
def test_hedge_long_and_short_identity_resolution():
    row = {"position_mode": "HEDGE", "position_side": "LONG"}
    assert resolve_position_identity(row, {"info": {"positionSide": "LONG"}},
                                     exchange_mode="HEDGE", trade_side="BUY") == ("HEDGE", "LONG", None)
    assert resolve_position_identity(row, {"info": {"positionSide": "LONG"}},
                                     exchange_mode="HEDGE", trade_side="SELL")[2] is not None
    assert resolve_position_identity({"position_mode": "HEDGE", "position_side": "SHORT"},
                                     {"info": {"positionSide": "SHORT"}},
                                     exchange_mode="HEDGE", trade_side="SELL") == ("HEDGE", "SHORT", None)


def test_one_way_long_and_short_use_both():
    assert resolve_position_identity({"position_mode": "ONE_WAY", "position_side": "BOTH"},
                                     {"info": {}}, exchange_mode="ONE_WAY", trade_side="BUY") == ("ONE_WAY", "BOTH", None)
    assert resolve_position_identity({"position_mode": "ONE_WAY", "position_side": "BOTH"},
                                     {"info": {}}, exchange_mode="ONE_WAY", trade_side="SELL") == ("ONE_WAY", "BOTH", None)
    # A persisted hedge leg can never be reinterpreted as one-way BOTH.
    assert resolve_position_identity({"position_mode": "ONE_WAY", "position_side": "LONG"},
                                     {"info": {}}, exchange_mode="ONE_WAY", trade_side="BUY")[2] is not None


# --------------------------------------------------------------------------- #
# 8, 9, 10: partial TP1, TP1 failure+retry, TP2 after TP1
# --------------------------------------------------------------------------- #
def test_partial_tp1_fill_with_exchange_evidence_reconciles(tmp_path, monkeypatch):
    # Journal recorded 1.0 remaining; BingX proves 0.5 AND a filled bot TP1 reduce
    # order is present in the closed-orders history. Only then is TP1 promoted.
    # The recorded close-order id is stored in the JOURNAL SNAPSHOT (not in STATE)
    # so this also proves recovery reads evidence from the journal.
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={
        "remaining_qty": 1.0, "bot_close_order_ids": ["close-tp1"]})

    class WithTp1Evidence(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [{"id": "close-tp1", "status": "closed", "side": "sell",
                     "filled": 1.0, "average": 105.0, "symbol": "BTC-USDT",
                     "info": {"orderId": "close-tp1", "type": "MARKET",
                              "positionSide": "LONG", "symbol": "BTC-USDT"}}]

    ex = WithTp1Evidence(position=_long_position(qty=0.5), open_orders=[_native_stop(qty=0.5)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    # Deliberately leave STATE.bot_close_order_ids empty: evidence must come from
    # the journal, not from the live (pre-restart) STATE.
    assert not ggpbot.STATE.get("bot_close_order_ids")
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["remaining_qty"] == 0.5
    assert ggpbot.STATE["tp1_hit"] is True


def test_tp1_evidence_rejects_wrong_position_side(tmp_path, monkeypatch):
    # The recorded close order is filled and matches the side/quantity but belongs
    # to the opposite hedge leg: it must NOT be accepted as this trade's TP1.
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={"remaining_qty": 1.0})

    class WrongLegEvidence(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [{"id": "close-tp1", "status": "closed", "side": "sell",
                     "filled": 1.0, "average": 105.0,
                     "info": {"orderId": "close-tp1", "type": "MARKET", "positionSide": "SHORT"}}]

    ex = WrongLegEvidence(position=_long_position(qty=0.5), open_orders=[_native_stop(qty=0.5)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    ggpbot.STATE["bot_close_order_ids"] = ["close-tp1"]
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["tp1_hit"] is False
    assert "unverified_position_reduction_requires_tp1_fill_evidence" in str(ggpbot.STATE["recovery_reason"])


def test_tp1_evidence_rejects_wrong_fill_quantity(tmp_path, monkeypatch):
    # A filled bot close order with a quantity far from the expected 50% TP1 size is
    # not a TP1 fill (e.g. a small partial or a different close).
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={"remaining_qty": 1.0})

    class WrongQtyEvidence(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [{"id": "close-tp1", "status": "closed", "side": "sell",
                     "filled": 0.1, "average": 105.0,
                     "info": {"orderId": "close-tp1", "type": "MARKET", "positionSide": "LONG"}}]

    ex = WrongQtyEvidence(position=_long_position(qty=0.5), open_orders=[_native_stop(qty=0.5)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    ggpbot.STATE["bot_close_order_ids"] = ["close-tp1"]
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["tp1_hit"] is False


def test_manual_or_external_reduction_is_not_relabelled_as_tp1(tmp_path, monkeypatch):
    # Journal recorded 1.0 remaining; BingX proves 0.5 but there is NO evidence of a
    # bot TP1 order. This could be a manual/external close, so recovery must Hold
    # rather than silently record TP1 and change the management stage.
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={"remaining_qty": 1.0})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    # MockExchange.fetch_closed_orders returns [] by default -> no TP1 evidence.
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(qty=0.5),
                                                   open_orders=[_native_stop(qty=0.5)]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["recovery_hold"] is True
    assert "unverified_position_reduction_requires_tp1_fill_evidence" in str(ggpbot.STATE["recovery_reason"])
    assert ggpbot.STATE["tp1_hit"] is False
    assert ggpbot.RECOVERY_BLOCK_ENTRIES is True


def test_recorded_tp1_hit_before_crash_still_restores(tmp_path, monkeypatch):
    # The journal itself already recorded tp1_hit before the crash: no re-derivation
    # is needed, and the reduced remainder is restored without holding.
    journal, _ = _seed_journal(tmp_path, snapshot_overrides={"remaining_qty": 1.0,
                                                             "tp1_hit": True})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(qty=0.5),
                                                   open_orders=[_native_stop(qty=0.5)]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["tp1_hit"] is True
    assert ggpbot.STATE["remaining_qty"] == 0.5


def test_tp1_close_failure_then_retry_is_idempotent(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)

    class CloseFailsOnce(MockExchange):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = 0

        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            if kind == "market":
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient network error")
            return super().create_order(symbol, kind, side, amount, price, params)

    ex = CloseFailsOnce(position=_long_position(), open_orders=[_native_stop()])
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id, "native_sl_order_id": "111"})
    # First attempt is ambiguous: it must NOT be treated as success and must block.
    assert ggpbot.safe_close_position("BTC/USDT:USDT", close_reason="TP1") is False
    assert ggpbot.STATE["execution_unresolved"] is True
    # A second blind MARKET close is refused while the outcome is unresolved.
    assert ggpbot.safe_close_position("BTC/USDT:USDT", close_reason="TP1") is False


def test_tp2_runs_only_after_tp1_is_verified(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path, snapshot_overrides={"tp1_hit": True,
                                                                   "remaining_qty": 1.0})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(qty=1.0),
                                                   open_orders=[_native_stop(qty=1.0)]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["tp1_hit"] is True
    assert ggpbot.STATE["tp2_hit"] is False


# --------------------------------------------------------------------------- #
# 11: duplicate runtime event must not double-act
# --------------------------------------------------------------------------- #
def test_runtime_recovery_is_idempotent_on_repeat_call(tmp_path, monkeypatch):
    journal, _ = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_long_position(),
                                                   open_orders=[_native_stop()]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    first_hold = ggpbot.STATE["recovery_hold"]
    # A second immediate recovery call must not corrupt the verified state.
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["recovery_hold"] == first_hold is False
    assert ggpbot.STATE["native_sl_status"] == "VERIFIED"


# --------------------------------------------------------------------------- #
# 12 & 13: timeout after close and accepted-but-still-open close
# --------------------------------------------------------------------------- #
def test_close_timeout_blocks_and_does_not_resubmit(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)

    class TimeoutClose(MockExchange):
        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            if kind == "market":
                self.create_calls.append((symbol, kind, side, amount, params or {}))
                raise TimeoutError("timeout after submission")
            return super().create_order(symbol, kind, side, amount, price, params)

    ex = TimeoutClose(position=_long_position(), open_orders=[_native_stop()])
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id})
    assert ggpbot.safe_close_position("BTC/USDT:USDT", close_reason="TP1") is False
    assert len([c for c in ex.create_calls if c[1] == "market"]) == 1
    assert ggpbot.STATE["execution_unresolved"] is True


def test_accepted_close_that_leaves_position_open_is_not_verified(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)

    class NoDeltaClose(MockExchange):
        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            if kind == "market":
                self.create_calls.append((symbol, kind, side, amount, params or {}))
                return {"id": "mk-nodelta", "status": "closed", "filled": amount, "average": 100.0}
            return super().create_order(symbol, kind, side, amount, price, params)

        def fetch_order(self, order_id, symbol=None, params=None):
            if str(order_id) == "mk-nodelta":
                return {"id": "mk-nodelta", "status": "closed", "filled": 0.0}
            return super().fetch_order(order_id, symbol, params)

    # The exchange still reports the full 2.0 position, so no reduction happened.
    ex = NoDeltaClose(position=_long_position(qty=2.0), open_orders=[_native_stop()])
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id})
    assert ggpbot.safe_close_position("BTC/USDT:USDT", close_reason="TP1") is False
    assert ggpbot.STATE["close_verified"] is False


# --------------------------------------------------------------------------- #
# 14: native SL modification failure must keep protection honest
# --------------------------------------------------------------------------- #
def test_native_sl_move_failure_retains_previous_protection(tmp_path, monkeypatch):
    # Post-entry modification is intentionally a no-op: the original verified
    # BingX stop remains unchanged and no order-management API is called.
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    ex = MockExchange(hedged=True, position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, fail_cancel=True)
    monkeypatch.setattr(ggpbot, "ex", ex)
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id, "position_mode": "HEDGE", "position_side": "LONG",
                         "native_sl_order_id": "111", "native_sl_verified": True,
                         "native_sl_status": "VERIFIED", "native_sl_original_price": 95.0,
                         "recovery_hold": False})
    assert ggpbot.move_native_stop_on_exchange("BTC/USDT:USDT", 100.5, 2.0,
                                               reason="BREAKEVEN") is False
    assert ggpbot.STATE["native_sl_verified"] is True
    assert ggpbot.STATE["native_sl_status"] == "VERIFIED"
    assert ggpbot.STATE["native_sl_original_price"] == pytest.approx(95.0)
    assert ggpbot.STATE["recovery_hold"] is False
    assert ggpbot.STATE["native_sl_order_id"] == "111"
    assert ex.cancel_calls == []
    assert ex.cancel_replace_calls == []
    assert ex.create_calls == []
    assert len(ex.open_orders) == 1


def test_native_sl_move_attempt_never_cancels_or_replaces_original_stop(tmp_path, monkeypatch):
    # Even if an exchange mock would allow Cancel/Replace, the production
    # policy forbids post-entry native-stop movement.
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)

    ex = MockExchange(hedged=True, position=_long_position(),
                      open_orders=[_native_stop(order_id="111")], cancel_replace=True)
    monkeypatch.setattr(ggpbot, "ex", ex)
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id, "position_mode": "HEDGE", "position_side": "LONG",
                         "native_sl_order_id": "111", "native_sl_verified": True,
                         "native_sl_status": "VERIFIED", "native_sl_original_price": 95.0,
                         "recovery_hold": False})
    assert ggpbot.move_native_stop_on_exchange("BTC/USDT:USDT", 100.5, 2.0,
                                               reason="BREAKEVEN") is False
    assert ggpbot.STATE["recovery_hold"] is False
    assert ggpbot.STATE["native_sl_verified"] is True
    assert ggpbot.STATE["native_sl_order_id"] == "111"
    assert ggpbot.STATE["native_sl_original_price"] == pytest.approx(95.0)
    assert ex.cancel_calls == []
    assert ex.cancel_replace_calls == []
    assert ex.create_calls == []
    assert [order["id"] for order in ex.open_orders] == ["111"]


def test_native_sl_move_attempt_leaves_journal_protection_unchanged(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    ex = MockExchange(hedged=True, position=_long_position(), open_orders=[_native_stop()])
    monkeypatch.setattr(ggpbot, "ex", ex)
    ggpbot.STATE.update({"open": True, "side": "BUY", "current_symbol": "BTC/USDT:USDT",
                         "entry": 100.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 2.0,
                         "trade_id": trade_id, "position_mode": "HEDGE", "position_side": "LONG",
                         "native_sl_order_id": "111", "native_sl_verified": True,
                         "native_sl_status": "VERIFIED", "native_sl_original_price": 95.0,
                         "recovery_hold": False})
    assert ggpbot.move_native_stop_on_exchange("BTC/USDT:USDT", 100.5, 2.0,
                                               reason="BREAKEVEN") is False
    row = journal.get(trade_id)
    assert row["state"]["native_sl_original_price"] == pytest.approx(95.0)
    assert row["state"]["native_sl_order_id"] == "111"
    assert ex.cancel_replace_calls == []
    assert [order["id"] for order in ex.open_orders] == ["111"]


# --------------------------------------------------------------------------- #
# 15: journal recovery after restart
# --------------------------------------------------------------------------- #
def test_journal_survives_restart_and_reloads_state(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path, snapshot_overrides={"tp1_hit": True,
                                                                   "remaining_qty": 1.0})
    # Re-open the same file to emulate a process restart.
    reopened = TradeJournal(str(Path(tmp_path) / "trade.sqlite3"))
    row = reopened.get(trade_id)
    assert row["state"]["tp1_hit"] is True
    assert row["remaining_qty"] == 1.0
    assert row["state"]["synthetic_sl"] == 95.0


# --------------------------------------------------------------------------- #
# Trade-lock lifecycle: a lock is released only by safe full-close finalization
# --------------------------------------------------------------------------- #
def test_verified_full_close_finalizer_releases_active_trade_lock(monkeypatch):
    _fresh_state()
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", True)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", None)
    monkeypatch.setattr(ggpbot, "tg_close", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "clear_position_dashboard", lambda: None)
    monkeypatch.setattr(ggpbot, "update_stats", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "_ACTIVE_TRADE", True)
    ggpbot.RECOVERY_BLOCK_ENTRIES = False
    ggpbot.MEMORY["position_status"] = "FLAT"
    ggpbot.STATE.update({
        "open": True, "close_verified": True, "current_symbol": "BTC/USDT:USDT",
        "side": "BUY", "entry": 100.0, "qty": 0.0, "initial_qty": 1.0,
        "remaining_qty": 0.0, "exchange_qty": 0.0, "trade_id": None,
        "entry_time": time.time(), "last_close_fill_price": 101.0,
        "last_close_realized_pnl": 1.0, "last_close_pnl_source": "BINGX_ORDER_REALIZED_PNL",
        "last_close_pnl_complete": True, "last_close_reason": "TEST_VERIFIED_CLOSE",
        "execution_unresolved": False, "close_reconciliation_required": False,
        "recovery_hold": False, "initial_margin": 5.0,
    })
    ggpbot.TRADE_STATE.update({"in_position": True, "symbol": "BTC/USDT:USDT"})
    try:
        result = ggpbot.finalize_trade_with_reality("BTC/USDT:USDT", "TEST_VERIFIED_CLOSE")
        assert result[0] == pytest.approx(1.0)
        assert result[1] == pytest.approx(20.0)
        assert ggpbot.STATE["open"] is False
        assert ggpbot.TRADE_STATE["in_position"] is False
        assert ggpbot._ACTIVE_TRADE is False
    finally:
        ggpbot.RECOVERY_BLOCK_ENTRIES = False
        ggpbot.MEMORY["position_status"] = "FLAT"
        ggpbot._ACTIVE_TRADE = False


@pytest.mark.parametrize("blocked_flag", [
    "execution_unresolved", "close_reconciliation_required", "recovery_hold",
])
def test_verified_full_close_retains_lock_if_recovery_gate_remains(monkeypatch, blocked_flag):
    _fresh_state()
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", True)
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", None)
    monkeypatch.setattr(ggpbot, "tg_close", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "clear_position_dashboard", lambda: None)
    monkeypatch.setattr(ggpbot, "update_stats", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "log_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr(ggpbot, "_ACTIVE_TRADE", True)
    ggpbot.RECOVERY_BLOCK_ENTRIES = False
    ggpbot.MEMORY["position_status"] = "FLAT"
    ggpbot.STATE.update({
        "open": True, "close_verified": True, "current_symbol": "BTC/USDT:USDT",
        "side": "BUY", "entry": 100.0, "qty": 0.0, "initial_qty": 1.0,
        "remaining_qty": 0.0, "exchange_qty": 0.0, "trade_id": None,
        "entry_time": time.time(), "last_close_fill_price": 101.0,
        "last_close_realized_pnl": 1.0, "last_close_pnl_source": "BINGX_ORDER_REALIZED_PNL",
        "last_close_pnl_complete": True, "last_close_reason": "TEST_VERIFIED_CLOSE",
        "execution_unresolved": False, "close_reconciliation_required": False,
        "recovery_hold": False, "initial_margin": 5.0,
    })
    ggpbot.STATE[blocked_flag] = True
    ggpbot.TRADE_STATE.update({"in_position": True, "symbol": "BTC/USDT:USDT"})
    try:
        ggpbot.finalize_trade_with_reality("BTC/USDT:USDT", "TEST_VERIFIED_CLOSE")
        assert ggpbot.STATE["open"] is False
        assert ggpbot._ACTIVE_TRADE is True
    finally:
        ggpbot.STATE[blocked_flag] = False
        ggpbot.RECOVERY_BLOCK_ENTRIES = False
        ggpbot.MEMORY["position_status"] = "FLAT"
        ggpbot._ACTIVE_TRADE = False


def test_open_position_reports_held_trade_lock_without_releasing_it(monkeypatch):
    _fresh_state()
    monkeypatch.setattr(ggpbot, "_ACTIVE_TRADE", True)
    messages = []
    monkeypatch.setattr(ggpbot, "log_execution", lambda message, *args, **kwargs: messages.append(str(message)))
    assert ggpbot.open_position("BUY", 1.0, "BTC/USDT:USDT") is None
    assert ggpbot._ACTIVE_TRADE is True
    assert any("[ENTRY_LOCKED]" in message for message in messages)


# --------------------------------------------------------------------------- #
# 16: no new entries while Recovery Hold is active
# --------------------------------------------------------------------------- #
def test_no_new_entry_during_recovery_hold(monkeypatch):
    monkeypatch.setattr(ggpbot, "refresh_unified_institutional_watchlist", lambda: None)
    ggpbot.RECOVERY_BLOCK_ENTRIES = True
    ggpbot.STATE.update({"open": True, "recovery_hold": True})
    assert ggpbot.smart_opportunity_selection() is False


def test_no_new_entry_when_position_status_unknown(monkeypatch):
    monkeypatch.setattr(ggpbot, "refresh_unified_institutional_watchlist", lambda: None)
    ggpbot.RECOVERY_BLOCK_ENTRIES = False
    ggpbot.STATE.update({"open": False})
    ggpbot.MEMORY["position_status"] = "UNKNOWN"
    try:
        assert ggpbot.smart_opportunity_selection() is False
    finally:
        ggpbot.MEMORY["position_status"] = "FLAT"


# --------------------------------------------------------------------------- #
# 17 & 18: Medium = 20% and Strong = 30% of available balance at 20x
# --------------------------------------------------------------------------- #
def test_medium_allocation_is_twenty_percent_of_available_balance():
    result = calculate_position_size(
        classification="TREND", available_balance=1000.0, entry_price=100.0,
        stop_price=99.99, side="BUY", leverage=20, contract_size=1.0,
        max_trade_risk_pct=1.0, stop_execution_buffer_pct=0.0)
    assert result.allocation_pct == pytest.approx(0.20)
    assert result.target_margin == pytest.approx(200.0)
    assert result.target_notional == pytest.approx(4000.0)
    assert result.margin_required == pytest.approx(200.0)
    assert allocation_for_classification("TREND")[0] == pytest.approx(0.20)


def test_strong_allocation_is_thirty_percent_only_with_authorization():
    result = calculate_position_size(
        classification="SNIPER", available_balance=1000.0, entry_price=100.0,
        stop_price=99.99, side="BUY", leverage=20, contract_size=1.0,
        max_trade_risk_pct=1.0, stop_execution_buffer_pct=0.0)
    assert result.allocation_pct == pytest.approx(0.30)
    assert result.target_margin == pytest.approx(300.0)
    assert result.margin_required == pytest.approx(300.0)
    # A bare SNIPER label without Watchlist authorization is downgraded to 20%.
    assert effective_sizing_classification("SNIPER", strong_tier_authorized=False) == "TREND"


def test_strong_tier_requires_independent_evidence_not_label():
    weak = {"score": 90, "volume_confirmed": False,
            "market_structure": {"confirmed": True},
            "order_block": {"strength": 6, "distance_atr": 1.0, "status": "FRESH"},
            "decision_setup_type": "REVERSAL_ENGINE"}
    strong = {**weak, "volume_confirmed": True}
    assert is_strong_candidate(strong) is True
    assert is_strong_candidate(weak) is False


# --------------------------------------------------------------------------- #
# 19 & 20: contract size / precision respected and risk cap never exceeded
# --------------------------------------------------------------------------- #
def test_contract_size_and_precision_are_respected():
    result = calculate_position_size(
        classification="TREND", available_balance=1000.0, entry_price=100.0,
        stop_price=100.01, side="SELL", leverage=20, contract_size=0.01,
        max_trade_risk_pct=1.0, stop_execution_buffer_pct=0.0)
    assert result.quantity * result.contract_size * 100 == pytest.approx(result.final_notional)
    # The notional is the min of the margin ceiling and the risk-permitted size.
    assert result.final_notional <= result.target_notional + 1e-9


def test_risk_cap_is_authoritative_and_never_raised_for_margin():
    result = calculate_position_size(
        classification="SNIPER", available_balance=10.0, entry_price=100.0,
        stop_price=99.0, side="BUY", leverage=20, contract_size=1.0,
        max_trade_risk_pct=1.0, stop_execution_buffer_pct=0.0)
    # Wide stop forces the size down below the 30% margin ceiling.
    assert result.margin_required < result.target_margin
    assert result.estimated_risk_pct <= 1.0 + 1e-9
    assert result.estimated_loss_at_stop <= result.max_risk_usdt + 1e-9


# --------------------------------------------------------------------------- #
# Entry-fill reconciliation (the RECOVERY_HOLD root-cause fix)
# --------------------------------------------------------------------------- #
def test_entry_fill_reconciliation_uses_only_exchange_evidence():
    row = {"trade_id": "T", "symbol": "BTC/USDT:USDT", "side": "BUY", "state": {}}
    order = {"id": "E-1", "status": "closed", "filled": 1.5, "average": 100.25}
    updates, error = reconcile_entry_fill(row, order, exchange_open_qty=1.5)
    assert error is None
    assert updates["actual_filled_qty"] == 1.5
    assert updates["entry"] == 100.25
    # No fill evidence -> no fabricated quantity.
    updates, error = reconcile_entry_fill(row, {"status": "closed", "filled": 0})
    assert not updates and error


def test_entry_pending_with_proven_fill_does_not_stay_in_hold(tmp_path, monkeypatch):
    # Row crashed before the post-fill checkpoint: actual_filled_qty is missing,
    # but the exchange entry order proves the fill. Recovery must NOT hold.
    journal, trade_id = _seed_journal(tmp_path, snapshot_overrides={
        "actual_filled_qty": 0.0, "entry_execution_state": "ENTRY_PENDING"})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      entry_order={"id": "E-1", "clientOrderId": "GGP-CLIENT",
                                   "status": "closed", "filled": 2.0, "average": 100.0})
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["recovery_hold"] is False
    assert ggpbot.STATE["actual_filled_qty"] == 2.0


def test_entry_pending_without_fill_evidence_still_holds(tmp_path, monkeypatch):
    journal, trade_id = _seed_journal(tmp_path, snapshot_overrides={
        "actual_filled_qty": 0.0, "entry_execution_state": "ENTRY_PENDING"})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    # No entry order retrievable -> no proof -> must stay in Hold.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()], entry_order=None)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["recovery_hold"] is True


# --------------------------------------------------------------------------- #
# Native-stop replacement gateway (direct, with mock exchange)
# --------------------------------------------------------------------------- #
def test_replace_native_stop_uses_cancel_replace_and_verifies():
    ex = MockExchange(hedged=True, position=_long_position(),
                      open_orders=[_native_stop(order_id="old-sl")])
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.HEDGE, position_side="LONG", previous_order_id="old-sl",
        previous_trigger_price=95.0, previous_quantity=2.0)
    assert result.moved is True
    assert result.order_id == "sl-replaced"
    assert ex.cancel_replace_calls
    assert all(str(o.get("id")) != "old-sl" for o in ex.open_orders)


def test_replace_native_stop_falls_back_to_cancel_then_place():
    ex = MockExchange(hedged=False, position=_long_position(),
                      open_orders=[_native_stop(order_id="old-sl",
                                                position_side="BOTH")],
                      cancel_replace=False)
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.ONE_WAY, position_side="BOTH", previous_order_id="old-sl",
        previous_trigger_price=95.0, previous_quantity=2.0)
    assert result.moved is True
    assert ex.cancel_calls == [("old-sl", "BTC/USDT:USDT")]


def test_replace_native_stop_is_fail_closed_when_cancel_ambiguous():
    # The cancel fails, so the OLD stop is still live. The result must be RETAINED
    # (protection preserved), never LOST, and never a fabricated MOVED.
    ex = MockExchange(hedged=True, open_orders=[_native_stop(order_id="old-sl")],
                      cancel_replace=False, fail_cancel=True)
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.HEDGE, position_side="LONG", previous_order_id="old-sl",
        previous_trigger_price=95.0, previous_quantity=2.0)
    assert result.outcome == "RETAINED"
    assert result.order_id == "old-sl"
    assert result.protected is True


def test_replace_native_stop_cancel_replace_failure_does_not_lose_old_stop():
    # cancelReplace raises before doing anything; the old stop must remain verified.
    class CancelReplaceRaises(MockExchange):
        def swapV1PrivatePostTradeCancelReplace(self, params=None):
            raise TimeoutError("cancelReplace transport timeout")

    ex = CancelReplaceRaises(hedged=True, open_orders=[_native_stop(order_id="old-sl")])
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.HEDGE, position_side="LONG", previous_order_id="old-sl",
        previous_trigger_price=95.0, previous_quantity=2.0)
    assert result.protected is True
    assert result.order_id == "old-sl"
    # The old stop is still present on the exchange.
    assert any(str(o.get("id")) == "old-sl" for o in ex.open_orders)


def test_replace_native_stop_never_creates_a_duplicate_stop():
    # cancelReplace silently does nothing (old stop stays). A naive implementation
    # could submit a second stop; place_verified must adopt the existing one.
    class NoOpCancelReplace(MockExchange):
        def swapV1PrivatePostTradeCancelReplace(self, params=None):
            return {"code": 0, "data": {"replaceResult": "false", "replaceMsg": "rejected"}}

    ex = NoOpCancelReplace(hedged=True, open_orders=[_native_stop(order_id="old-sl")])
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.HEDGE, position_side="LONG", previous_order_id="old-sl",
        previous_trigger_price=95.0, previous_quantity=2.0)
    assert result.protected is True
    stop_orders = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stop_orders) == 1, f"expected a single stop, found {len(stop_orders)}"


def test_replace_native_stop_replaces_stale_mismatched_stop_and_verifies_quantity():
    # An active stop exists but with the WRONG quantity (stale after a partial
    # close). The move must not adopt it as verified; it must cancel it and place
    # a correctly-sized stop, and the returned id must match the new level/qty.
    ex = MockExchange(hedged=False, position=_long_position(), cancel_replace=False,
                      open_orders=[_native_stop(order_id="stale", qty=1.0, position_side="BOTH")])
    service = TradeExecutionService(ex)
    result = service.replace_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=100.5,
        mode=PositionMode.ONE_WAY, position_side="BOTH", previous_order_id=None,
        previous_trigger_price=None, previous_quantity=None)
    assert result.moved is True
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1
    assert float(stops[0]["stopPrice"]) == 100.5
    assert float(stops[0]["quantity"]) == 2.0
    assert str(stops[0]["id"]) != "stale"


# --------------------------------------------------------------------------- #
# Hedge SHORT restart (mirror of scenario 6)
# --------------------------------------------------------------------------- #
def test_restart_hedge_short_position_restores(tmp_path, monkeypatch):
    journal, _ = _seed_journal(tmp_path, side="SELL", position_side="SHORT",
                               entry=100.0, sl=105.0, tp1=95.0, tp2=90.0,
                               snapshot_overrides={"side": "SELL", "position_side": "SHORT",
                                                   "synthetic_sl": 105.0, "native_sl_original_price": 105.0,
                                                   "sl": 105.0, "tp1_price": 95.0, "tp2_price": 90.0,
                                                   "synthetic_tp1": 95.0})
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    stop = {"id": "111", "status": "NEW", "symbol": "BTC-USDT", "type": "STOP_MARKET",
            "side": "BUY", "positionSide": "SHORT", "stopPrice": "105.0", "origQty": "2.0"}
    monkeypatch.setattr(ggpbot, "ex", MockExchange(position=_short_position(), open_orders=[stop]))
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["side"] == "SELL"
    assert ggpbot.STATE["native_sl_status"] == "VERIFIED"
