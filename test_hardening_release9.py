"""Release 9 hardening: TP1 evidence integrity + native stop replacement state machine.

Drives the real recovery and native-stop code paths against a deterministic mock
exchange. No network, no live keys. Covers the review findings:

  * TP1 evidence must match position identity per account mode and must be sourced
    from the persisted journal (survives restart), not from live STATE.
  * Native stop replacement is an explicit PRECHECK / REPLACEMENT / POSTCHECK
    state machine with MOVED / RETAINED / LOST outcomes that are never inferred
    from a stale acknowledgement or a cancel ambiguity.
"""
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("PAPER_MODE", "true")
os.environ.setdefault("BINGX_API_KEY", "")
os.environ.setdefault("BINGX_API_SECRET", "")

import ggpbot
from bingx_execution import PositionMode, StopMoveResult, TradeExecutionService
from roro_trade_journal import TradeJournal


# --------------------------------------------------------------------------- #
# Mock exchange (native stop focused)
# --------------------------------------------------------------------------- #
class MockExchange:
    def __init__(self, *, hedged=True, position=None, open_orders=None,
                 cancel_replace=True, fail_cancel=False, create_stop_raises=False,
                 cancel_replace_raises=False, mark_price=None):
        self.hedged = hedged
        self.position = position
        self.open_orders = list(open_orders or [])
        self.cancel_replace = cancel_replace
        self.fail_cancel = fail_cancel
        self.create_stop_raises = create_stop_raises
        self.cancel_replace_raises = cancel_replace_raises
        self.mark_price = mark_price
        if not cancel_replace:
            self.swapV1PrivatePostTradeCancelReplace = None
        self.create_calls = []
        self.cancel_calls = []
        self.cancel_replace_calls = []
        self._markets = {"BTC/USDT:USDT": {"id": "BTC-USDT", "contractSize": 1.0,
                                           "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}}

    def load_markets(self):
        return self._markets

    def market(self, symbol):
        return self._markets.get(symbol) or {"id": symbol.split("/")[0] + "-USDT", "contractSize": 1.0,
                                             "limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}}}

    def amount_to_precision(self, symbol, amount):
        return float(amount)

    def fetch_position_mode(self, symbol=None):
        return {"hedged": self.hedged}

    def fetch_positions(self, symbols=None):
        return [self.position] if self.position else []

    def fetch_ticker(self, symbol, params=None):
        mark = self.mark_price
        if mark is None and isinstance(self.position, dict):
            mark = self.position.get("markPrice")
        return {"last": mark, "close": mark}

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return list(self.open_orders)

    def swapV2PrivateGetTradeOpenOrders(self, params=None):
        return {"code": 0, "data": {"orders": []}}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.create_calls.append((symbol, kind, side, amount, params or {}))
        if kind == "STOP_MARKET":
            if self.create_stop_raises:
                raise RuntimeError("exchange rejected STOP_MARKET")
            order_id = f"sl-new-{len(self.create_calls)}"
            self.open_orders.append({
                "id": order_id, "status": "NEW", "symbol": symbol, "type": "STOP_MARKET",
                "side": side, "positionSide": (params or {}).get("positionSide", "BOTH"),
                "stopPrice": str((params or {}).get("stopPrice")), "origQty": str(amount),
            })
            return {"id": order_id, "status": "NEW"}
        return {"id": "mk-1", "status": "closed", "filled": amount, "average": 100.0}

    def cancel_order(self, order_id, symbol=None, params=None):
        self.cancel_calls.append((str(order_id), symbol))
        if self.fail_cancel:
            raise TimeoutError("cancel timeout")
        self.open_orders = [o for o in self.open_orders if str(o.get("id")) != str(order_id)]
        return {"id": order_id, "status": "CANCELED"}

    def swapV1PrivatePostTradeCancelReplace(self, params=None):
        self.cancel_replace_calls.append(params)
        if self.cancel_replace_raises:
            raise TimeoutError("cancelReplace timeout")
        old = str((params or {}).get("cancelOrderId"))
        self.open_orders = [o for o in self.open_orders if str(o.get("id")) != old]
        new_id = "sl-replaced"
        self.open_orders.append({
            "id": new_id, "status": "NEW", "symbol": "BTC/USDT:USDT", "type": "STOP_MARKET",
            "side": str((params or {}).get("side", "SELL")).lower(),
            "positionSide": (params or {}).get("positionSide", "BOTH"),
            "stopPrice": str((params or {}).get("stopPrice")),
            "origQty": str((params or {}).get("quantity")),
        })
        return {"code": 0, "data": {"replaceResult": "true", "newOrderResponse": {"orderId": new_id}}}


def _long_position(qty=2.0, entry=100.0, mark=101.0, position_side="LONG"):
    return {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": qty, "entryPrice": entry,
            "markPrice": mark, "info": {"positionSide": position_side, "positionAmt": str(qty),
                                        "avgPrice": str(entry)}}


def _native_stop(qty=2.0, trigger=95.0, position_side="LONG", side="SELL", order_id="old-sl",
                 status="NEW"):
    return {"id": order_id, "status": status, "symbol": "BTC-USDT", "type": "STOP_MARKET",
            "side": side, "positionSide": position_side, "stopPrice": str(trigger), "origQty": str(qty)}


# --------------------------------------------------------------------------- #
# Native stop replacement — MOVED / RETAINED / LOST
# --------------------------------------------------------------------------- #
def _move(ex, **over):
    kwargs = dict(symbol="BTC/USDT:USDT", side="BUY", quantity=2.0, new_trigger_price=99.0,
                  mode=PositionMode.HEDGE, position_side="LONG", previous_order_id="old-sl",
                  previous_trigger_price=95.0, previous_quantity=2.0)
    kwargs.update(over)
    return TradeExecutionService(ex).replace_native_stop(**kwargs)


def test_native_sl_moved_with_readback():
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()])
    result = _move(ex)
    assert result.outcome == "MOVED" and result.order_id == "sl-replaced"
    assert ex.cancel_replace_calls


def test_native_sl_cancel_failure_retains_verified_old_stop():
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, fail_cancel=True)
    result = _move(ex)
    assert result.outcome == "RETAINED" and result.order_id == "old-sl"
    assert result.protected is True


def test_native_sl_cancel_ok_but_new_create_fails_is_retained_or_lost_not_moved():
    # Cancel succeeds (old stop gone), the replacement cannot be created, and the
    # previous stop cannot be re-placed either -> LOST (never a false MOVED).
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, create_stop_raises=True)
    result = _move(ex)
    assert result.outcome in {"LOST", "RETAINED"}
    assert result.outcome != "MOVED"
    if result.outcome == "LOST":
        assert result.order_id is None


def test_native_sl_cancel_ok_new_create_fails_but_old_replaced_succeeds():
    # Cancel succeeds and the new stop is created fine -> MOVED with a fresh id.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()], cancel_replace=False)
    result = _move(ex)
    assert result.outcome == "MOVED"
    assert result.order_id and result.order_id != "old-sl"


def test_native_sl_timeout_after_cancel_does_not_claim_retained_blindly():
    # Ambiguous cancel (timeout) with the old stop still present -> RETAINED only
    # because a fresh read proves it; the reason records the ambiguity.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, fail_cancel=True)
    result = _move(ex)
    assert result.outcome == "RETAINED"
    assert "cancel_ambiguous" in result.reason


def test_native_sl_cancel_replace_transport_timeout_is_not_assumed():
    # cancelReplace times out (outcome unknown) and the old stop is still live: the
    # code must verify fresh, retain the proven old stop, and NOT create a new stop
    # on an unproven state.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace_raises=True)
    result = _move(ex)
    assert result.outcome == "RETAINED"
    assert result.order_id == "old-sl"
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1
    assert float(stops[0]["stopPrice"]) == 95.0  # the proven old level, unchanged


def test_native_sl_ambiguous_cancel_without_remaining_stop_is_not_retained():
    # The cancel "fails" but the old stop is actually gone and no new stop exists:
    # nothing can be proven active -> LOST (never RETAINED from ambiguity alone).
    ex = MockExchange(position=_long_position(), open_orders=[],
                      cancel_replace=False, fail_cancel=True)
    result = _move(ex)
    assert result.outcome == "LOST"
    assert result.order_id is None


def test_native_sl_retained_only_after_fresh_read():
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, fail_cancel=True)
    result = _move(ex)
    # RETAINED is backed by a read of the live open orders.
    assert result.outcome == "RETAINED"
    assert ex.open_orders and str(ex.open_orders[0].get("id")) == "old-sl"


def test_native_sl_precheck_rejects_old_stop_covering_less_than_position():
    # Old stop covers only 1.0 while the position is 2.0 -> precheck aborts; the
    # mismatched stop is not accepted as retained protection.
    ex = MockExchange(position=_long_position(qty=2.0),
                      open_orders=[_native_stop(qty=1.0)])
    result = _move(ex)
    assert result.outcome != "MOVED"


def test_native_sl_precheck_rejects_wrong_trigger_price():
    # The old stop is at 95 but we claim previous_trigger_price=94 -> not found.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop(trigger=95.0)])
    result = _move(ex, previous_trigger_price=94.0)
    assert result.outcome != "MOVED"
    assert "precheck" in result.reason


def test_native_sl_precheck_rejects_position_side_mismatch():
    # Hedge position is LONG but we attempt to move a SHORT-scoped stop.
    ex = MockExchange(position=_long_position(position_side="LONG"),
                      open_orders=[_native_stop(position_side="SHORT", side="BUY")])
    result = _move(ex, position_side="SHORT")
    assert result.outcome != "MOVED"


def test_native_sl_precheck_hedge_requires_position_side_on_position():
    # The position record carries no positionSide: Hedge PRECHECK must refuse.
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2.0, "entryPrice": 100.0,
           "markPrice": 101.0, "info": {"positionAmt": "2.0"}}
    ex = MockExchange(position=pos, open_orders=[_native_stop()])
    result = _move(ex)
    assert result.outcome != "MOVED"
    assert "precheck" in result.reason


def test_native_sl_ambiguous_cancel_does_not_create_new_stop():
    # The cancel raises AND the old stop is still live. No new stop may be created.
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False, fail_cancel=True)
    result = _move(ex)
    assert result.outcome == "RETAINED"
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1  # no duplicate created
    assert all(str(o.get("id")) == "old-sl" for o in stops)


def test_native_sl_no_duplicate_stop_created():
    ex = MockExchange(position=_long_position(), open_orders=[_native_stop()],
                      cancel_replace=False)
    _move(ex)
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1


def test_native_sl_cancel_returns_ok_but_stop_still_listed_blocks_creation():
    # cancel_order() returns without error but the stop is STILL in open orders
    # (the cancellation was not applied). No replacement may be created, and the
    # proven old stop must be retained.
    class CancelNoEffect(MockExchange):
        def cancel_order(self, order_id, symbol=None, params=None):
            self.cancel_calls.append((str(order_id), symbol))
            return {"id": order_id, "status": "CANCELED"}  # lies: does not remove

    ex = CancelNoEffect(position=_long_position(), open_orders=[_native_stop()],
                        cancel_replace=False)
    result = _move(ex)
    assert result.outcome == "RETAINED"
    assert result.order_id == "old-sl"
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1, "a duplicate stop must never be created"
    assert not any(c[1] == "STOP_MARKET" for c in ex.create_calls)


def test_native_sl_place_new_requires_proven_removal_of_stale_stop():
    # No previous id, but a stale (wrong-price) stop exists. cancel_order returns ok
    # yet the stop is still listed. place_new must NOT create the replacement.
    class CancelNoEffect(MockExchange):
        def cancel_order(self, order_id, symbol=None, params=None):
            self.cancel_calls.append((str(order_id), symbol))
            return {"id": order_id, "status": "CANCELED"}  # lies: does not remove

    ex = CancelNoEffect(position=_long_position(), cancel_replace=False,
                        open_orders=[_native_stop(order_id="stale", trigger=90.0)])
    result = _move(ex, previous_order_id=None, previous_trigger_price=None, previous_quantity=None)
    assert result.outcome == "LOST"
    assert not any(c[1] == "STOP_MARKET" for c in ex.create_calls)


def test_native_sl_place_new_creates_after_proven_removal_of_stale_stop():
    # Same as above, but the cancel actually removes the stale stop: the new stop
    # must then be created and verified.
    ex = MockExchange(position=_long_position(), cancel_replace=False,
                      open_orders=[_native_stop(order_id="stale", trigger=90.0)])
    result = _move(ex, previous_order_id=None, previous_trigger_price=None, previous_quantity=None)
    assert result.outcome == "MOVED"
    stops = [o for o in ex.open_orders if str(o.get("type")) == "STOP_MARKET"]
    assert len(stops) == 1
    assert float(stops[0]["stopPrice"]) == 99.0
    assert str(stops[0]["id"]) != "stale"


def test_native_sl_unreadable_open_orders_blocks_creation():
    # Open-orders read fails entirely: the state is UNKNOWN, so no stop may be
    # created (a failed read is never treated as "no stop exists").
    class Unreadable(MockExchange):
        def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
            raise RuntimeError("open orders unreadable")

        def swapV2PrivateGetTradeOpenOrders(self, params=None):
            raise RuntimeError("open orders unreadable")

    ex = Unreadable(position=_long_position(), open_orders=[_native_stop()], cancel_replace=False)
    result = _move(ex)
    assert result.outcome == "LOST"
    assert not any(c[1] == "STOP_MARKET" for c in ex.create_calls)


def test_native_sl_lost_when_no_stop_anywhere():
    ex = MockExchange(position=_long_position(), open_orders=[],
                      cancel_replace=False, create_stop_raises=True)
    result = _move(ex, previous_order_id=None, previous_trigger_price=None, previous_quantity=None)
    assert result.outcome == "LOST"


def test_native_sl_precheck_rejects_stop_on_wrong_side_of_market():
    # LONG position, mark 101; a new stop at 102 (above market) is invalid.
    ex = MockExchange(position=_long_position(mark=101.0), open_orders=[_native_stop()])
    result = _move(ex, new_trigger_price=102.0)
    assert result.outcome != "MOVED"
    assert "precheck" in result.reason


# --------------------------------------------------------------------------- #
# TP1 evidence integrity (position identity per account mode)
# --------------------------------------------------------------------------- #
def _tp1_probe(side="BUY", position_side="LONG", mode="HEDGE", expected=1.0, order=None,
               close_order_ids=None, entry_ts=None):
    class Ex(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [order] if order else []
    ggpbot.ex = Ex(position=_long_position())
    return ggpbot._tp1_fill_verified_on_exchange(
        "BTC/USDT:USDT", side, expected_fill_qty=expected, position_side=position_side,
        position_mode=mode, close_order_ids=close_order_ids or set(), trade_entry_ts=entry_ts)


def _tp1_order(position_side="LONG", side="SELL", filled=1.0, status="CLOSED", oid="tp1-1",
               symbol="BTC-USDT", type_="MARKET", ts=None):
    info = {"orderId": oid, "type": type_, "side": side, "positionSide": position_side,
            "status": status, "executedQty": str(filled), "symbol": symbol}
    if ts is not None:
        info["updateTime"] = ts
    return {"id": oid, "status": status, "side": side, "positionSide": position_side,
            "filled": filled, "symbol": symbol, "type": type_, "info": info}


def test_tp1_hedge_long_position_side_correct():
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0)
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is True


def test_tp1_hedge_short_position_side_correct():
    order = _tp1_order(position_side="SHORT", side="BUY", filled=1.0)
    assert _tp1_probe(side="SELL", position_side="SHORT", order=order,
                      close_order_ids={"tp1-1"}) is True


def test_tp1_hedge_rejects_missing_position_side():
    order = _tp1_order(position_side="", side="SELL", filled=1.0)
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_hedge_rejects_conflicting_position_side():
    order = _tp1_order(position_side="SHORT", side="SELL", filled=1.0)
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_one_way_accepts_both_or_absent():
    order_both = _tp1_order(position_side="BOTH", side="SELL", filled=1.0)
    assert _tp1_probe(mode="ONE_WAY", position_side="BOTH", order=order_both,
                      close_order_ids={"tp1-1"}) is True
    order_absent = _tp1_order(position_side="", side="SELL", filled=1.0)
    assert _tp1_probe(mode="ONE_WAY", position_side="BOTH", order=order_absent,
                      close_order_ids={"tp1-1"}) is True


def test_tp1_one_way_rejects_hedge_leg():
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0)
    assert _tp1_probe(mode="ONE_WAY", position_side="BOTH", order=order,
                      close_order_ids={"tp1-1"}) is False


def test_tp1_rejects_symbol_mismatch():
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0, symbol="ETH-USDT")
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_rejects_wrong_close_side():
    order = _tp1_order(position_side="LONG", side="BUY", filled=1.0)
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_rejects_wrong_filled_quantity():
    order = _tp1_order(position_side="LONG", side="SELL", filled=0.1)
    assert _tp1_probe(order=order, expected=1.0, close_order_ids={"tp1-1"}) is False


def test_tp1_rejects_order_not_linked_to_trade():
    # No recorded id and no TP/partial tag -> cannot link to this trade.
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0, type_="MARKET")
    assert _tp1_probe(order=order, close_order_ids=set()) is False


def test_tp1_unknown_mode_is_refused():
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0)
    assert _tp1_probe(mode="UNKNOWN", order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_rejects_missing_symbol():
    # An order without any symbol cannot be trusted as this trade's TP1.
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0, symbol="")
    assert _tp1_probe(order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_hedge_rejects_when_expected_side_unknown():
    # Hedge with no established expected leg must not accept any evidence.
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0)
    assert _tp1_probe(position_side="", order=order, close_order_ids={"tp1-1"}) is False


def test_tp1_tagged_order_before_entry_is_rejected_by_time():
    entry_ts = 1_700_000_000_000
    old_ts = entry_ts - 3_600_000  # one hour before entry
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0, type_="TAKE_PROFIT_MARKET",
                       ts=old_ts)
    # Tagged but predates the entry and has no recorded id -> rejected.
    assert _tp1_probe(order=order, close_order_ids=set(), entry_ts=entry_ts) is False


def test_tp1_tagged_order_after_entry_is_accepted_by_time():
    entry_ts = 1_700_000_000_000
    new_ts = entry_ts + 60_000
    order = _tp1_order(position_side="LONG", side="SELL", filled=1.0, type_="TAKE_PROFIT_MARKET",
                       ts=new_ts)
    assert _tp1_probe(order=order, close_order_ids=set(), entry_ts=entry_ts) is True


# --------------------------------------------------------------------------- #
# TP1 evidence must survive restart (journal-sourced, not STATE)
# --------------------------------------------------------------------------- #
def _seed_journal(tmp_path, *, position_side="LONG", side="BUY", journal_remaining=2.0,
                  bot_close_ids=None, tp1_hit=False):
    journal = TradeJournal(str(Path(tmp_path) / "t.sqlite3"))
    tid = "T-RESTART"
    journal.persist_entry_pending(
        trade_id=tid, client_order_id="C", symbol="BTC/USDT:USDT", side=side,
        position_mode="HEDGE", position_side=position_side, leverage=20, requested_qty=2.0,
        entry_ts=time.time(), status="ENTRY_PENDING", structural_stop=95.0,
        management_state={"trade_id": tid, "synthetic_sl": 95.0, "tp1_price": 105.0,
                          "tp2_price": 110.0, "initial_qty": 2.0, "qty": 2.0, "remaining_qty": 2.0})
    journal.record_execution(tid, exchange_order_id="E-1", filled_qty=2.0, remaining_qty=0.0,
                             average_fill_price=100.0, actual_entry_price=100.0, status="FILLED")
    journal.start_trade(tid, "BTC/USDT:USDT", side, "TREND", 2.0, 100.0, 95.0)
    # `journal_remaining` is the PRE-TP1 remaining recorded in the journal (the crash
    # happened before the post-fill checkpoint). The live exchange qty is passed by
    # each test and is smaller, which is the reduction the recovery must reconcile.
    state = {"open": True, "side": side, "entry": 100.0, "qty": 2.0, "initial_qty": 2.0,
             "exchange_qty": journal_remaining, "remaining_qty": journal_remaining, "sl": 95.0,
             "current_symbol": "BTC/USDT:USDT", "tp1_price": 105.0, "tp2_price": 110.0,
             "synthetic_sl": 95.0, "synthetic_tp1": 105.0, "native_sl_original_price": 95.0,
             "trade_id": tid, "position_mode": "HEDGE", "position_side": position_side,
             "native_sl_order_id": "111", "native_sl_verified": True, "native_sl_status": "VERIFIED",
             "recovery_hold": False, "tp1_hit": tp1_hit, "tp2_hit": False, "be_done": False,
             "trail_activated": False, "trail_stop": 0.0, "max_price": 100.0, "min_price": 100.0,
             "entry_time": time.time(), "trade_lifecycle_state": "LIVE"}
    if bot_close_ids:
        state["bot_close_order_ids"] = list(bot_close_ids)
    journal.save_snapshot(tid, state, event_key="entry-state:1", event_type="ENTRY_STATE")
    return journal


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot, "MODE_LIVE", True)
    yield


def _fresh_state():
    template = dict(ggpbot.STATE)
    ggpbot.STATE.clear()
    for key in template:
        ggpbot.STATE[key] = None
    for flag in ("open", "recovery_hold", "execution_unresolved", "close_reconciliation_required",
                 "close_verified", "native_sl_verified", "tp1_hit", "tp2_hit", "be_done",
                 "trail_activated", "partial_closed"):
        ggpbot.STATE[flag] = False
    ggpbot.RECOVERY_BLOCK_ENTRIES = True


def test_tp1_restored_from_journal_after_restart_without_state_ids(tmp_path, monkeypatch):
    # Real restart: the close-order id lives ONLY in the journal snapshot. STATE is
    # cleared, so recovery must source the evidence from the journal.
    journal = _seed_journal(tmp_path, journal_remaining=2.0, bot_close_ids=["close-tp1"])

    class Ex(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [_tp1_order(position_side="LONG", side="SELL", filled=1.0, oid="close-tp1")]

    ex = Ex(position=_long_position(qty=1.0), open_orders=[_native_stop(qty=1.0)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert not ggpbot.STATE.get("bot_close_order_ids")  # STATE has no ids
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["tp1_hit"] is True
    assert ggpbot.STATE["remaining_qty"] == 1.0


def test_tp1_not_restored_when_journal_has_no_ids_and_no_evidence(tmp_path, monkeypatch):
    # Quantity dropped but neither the journal nor the exchange proves a TP1 fill.
    journal = _seed_journal(tmp_path, journal_remaining=2.0, bot_close_ids=None)
    ex = MockExchange(position=_long_position(qty=1.0), open_orders=[_native_stop(qty=1.0)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["tp1_hit"] is False
    assert "unverified_position_reduction_requires_tp1_fill_evidence" in str(ggpbot.STATE["recovery_reason"])


def test_tp1_restore_is_idempotent_across_repeated_recovery(tmp_path, monkeypatch):
    journal = _seed_journal(tmp_path, journal_remaining=2.0, bot_close_ids=["close-tp1"])

    class Ex(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [_tp1_order(position_side="LONG", side="SELL", filled=1.0, oid="close-tp1")]

    ex = Ex(position=_long_position(qty=1.0), open_orders=[_native_stop(qty=1.0)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["tp1_hit"] is True
    remaining_after_first = ggpbot.STATE["remaining_qty"]
    # A second recovery pass must not double-apply TP1 or change the remainder.
    assert ggpbot.startup_trade_recovery() is True
    assert ggpbot.STATE["tp1_hit"] is True
    assert ggpbot.STATE["remaining_qty"] == remaining_after_first


def test_tp1_partial_fill_order_is_not_accepted_as_full_tp1(tmp_path, monkeypatch):
    # A PARTIALLY_FILLED order with the wrong fill size is not a completed TP1.
    journal = _seed_journal(tmp_path, journal_remaining=2.0, bot_close_ids=["close-tp1"])

    class Ex(MockExchange):
        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [_tp1_order(position_side="LONG", side="SELL", filled=0.2, oid="close-tp1",
                               status="PARTIALLY_FILLED")]

    ex = Ex(position=_long_position(qty=1.0), open_orders=[_native_stop(qty=1.0)])
    monkeypatch.setattr(ggpbot, "INSTITUTIONAL_TRADE_JOURNAL", journal)
    monkeypatch.setattr(ggpbot, "ex", ex)
    _fresh_state()
    assert ggpbot.startup_trade_recovery() is False
    assert ggpbot.STATE["tp1_hit"] is False
