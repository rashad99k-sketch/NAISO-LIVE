"""Centralized, fail-closed BingX futures execution primitives.

This module deliberately keeps exchange I/O injectable so all safety rules can be
unit-tested without submitting orders. It is the only module that should build
BingX position-side parameters for GGP entry/exit execution.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
import json
import math
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Optional

from ggp_position_sizing import extract_leverage_value


@dataclass(frozen=True)
class StopMoveResult:
    """Outcome of a native stop move.

    `outcome` is one of:
      - MOVED:    the new stop is verified active at the new level on a fresh read.
      - RETAINED: the move failed, but the previous stop is verified active on a
                  fresh read (still present, or re-established at the old level).
      - LOST:     no stop could be proven active at either level.
    `reason` is a short machine-readable explanation of the decision path.
    """
    outcome: str
    order_id: Optional[str] = None
    reason: str = ""

    @property
    def moved(self) -> bool:
        return self.outcome == "MOVED"

    @property
    def protected(self) -> bool:
        return self.outcome in {"MOVED", "RETAINED"}


class PositionMode(str, Enum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"
    UNKNOWN = "UNKNOWN"


class ExecutionState(str, Enum):
    ENTRY_PENDING = "ENTRY_PENDING"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTED = "REJECTED"
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
    ENTRY_PRICE_UNKNOWN = "ENTRY_PRICE_UNKNOWN"
    PROTECTION_HOLD = "PROTECTION_HOLD"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class ModeResult:
    mode: PositionMode
    reason: str
    raw: Any = None


@dataclass
class ExecutionResult:
    state: ExecutionState
    trade_id: str
    client_order_id: str
    exchange_order_id: Optional[str] = None
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    average_fill_price: Optional[float] = None
    actual_entry_price: Optional[float] = None
    position_mode: str = PositionMode.UNKNOWN.value
    position_side: Optional[str] = None
    native_sl_order_id: Optional[str] = None
    reason: str = ""
    raw_order: Any = None
    raw_position: Any = None
    order_terminal: bool = False
    submission_attempted: bool = False
    position_status: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                return number
        except (TypeError, ValueError):
            continue
    return None


def _nested_info(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    info = value.get("info")
    return info if isinstance(info, dict) else {}


def _extract_filled(order: Any) -> float:
    if not isinstance(order, dict):
        return 0.0
    info = _nested_info(order)
    return _first_number(order.get("filled"), order.get("executedQty"), order.get("cumExecQty"),
                         info.get("executedQty"), info.get("cumExecQty"), info.get("filled")) or 0.0


def _extract_remaining(order: Any, requested: float, filled: float) -> float:
    if isinstance(order, dict):
        info = _nested_info(order)
        remaining = _first_number(order.get("remaining"), order.get("remainingQty"),
                                  info.get("remaining"), info.get("remainingQty"))
        if remaining is not None:
            return remaining
    return max(0.0, float(requested) - float(filled))


def _extract_average(order: Any) -> Optional[float]:
    if not isinstance(order, dict):
        return None
    info = _nested_info(order)
    return _first_number(order.get("average"), order.get("avgPrice"), order.get("price"),
                         info.get("avgPrice"), info.get("average"))


def _status(order: Any) -> str:
    if not isinstance(order, dict):
        return "UNKNOWN"
    info = _nested_info(order)
    return str(order.get("status") or order.get("state") or info.get("status") or info.get("orderStatus") or "UNKNOWN").upper()


_TERMINAL_ENTRY_ORDER_STATES = {
    "CLOSED", "FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED",
}


def _position_quantity(position: Any) -> float:
    if not isinstance(position, dict):
        return 0.0
    info = _nested_info(position)
    for value in (position.get("contracts"), position.get("positionAmt"),
                  info.get("positionAmt"), info.get("contracts")):
        try:
            number = abs(float(value))
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError, OverflowError):
            continue
    return 0.0


def _position_entry_price(position: Any) -> Optional[float]:
    if not isinstance(position, dict):
        return None
    info = _nested_info(position)
    return _first_number(position.get("entryPrice"), position.get("average"),
                         info.get("avgPrice"), info.get("entryPrice"))


def _order_id(order: Any) -> Optional[str]:
    if not isinstance(order, dict):
        return None
    info = _nested_info(order)
    value = (order.get("id") or order.get("orderId") or order.get("orderID")
             or order.get("triggerOrderId") or order.get("trigger_order_id")
             or info.get("orderId") or info.get("orderID")
             or info.get("triggerOrderId") or info.get("trigger_order_id"))
    return str(value) if value not in (None, "") else None


def _weighted_trade_average(trades: Iterable[Dict[str, Any]]) -> Optional[float]:
    total_qty = total_value = 0.0
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        qty = _first_number(trade.get("amount"), trade.get("qty"), trade.get("filled"))
        price = _first_number(trade.get("price"), trade.get("avgPrice"))
        if qty and price:
            total_qty += qty
            total_value += qty * price
    return total_value / total_qty if total_qty > 0 else None


class BingXPositionModeAdapter:
    """Read-only account-mode adapter; it never calls set_position_mode."""

    def __init__(self, exchange: Any, logger: Optional[Callable[[str], None]] = None,
                 api_call: Optional[Callable[..., Any]] = None):
        self.exchange = exchange
        self.logger = logger or (lambda _message: None)
        self.api_call = api_call

    def _read(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self.api_call(method, *args, **kwargs) if self.api_call else method(*args, **kwargs)

    def read(self, symbol: Optional[str] = None) -> ModeResult:
        method = getattr(self.exchange, "fetch_position_mode", None)
        if not callable(method):
            return ModeResult(PositionMode.UNKNOWN, "fetch_position_mode unavailable")
        try:
            try:
                raw = self._read(method, symbol) if symbol is not None else self._read(method)
            except TypeError:
                raw = self._read(method)
        except Exception as exc:
            return ModeResult(PositionMode.UNKNOWN, f"position mode query failed: {type(exc).__name__}: {exc}")
        values = []
        if isinstance(raw, dict):
            values.extend([raw.get("hedged"), raw.get("dualSidePosition"), raw.get("dual_side_position")])
            info = raw.get("info")
            if isinstance(info, dict):
                values.extend([info.get("hedged"), info.get("dualSidePosition"), info.get("dual_side_position")])
                data = info.get("data")
                if isinstance(data, dict):
                    values.extend([data.get("hedged"), data.get("dualSidePosition")])
        for value in values:
            if isinstance(value, bool):
                return ModeResult(PositionMode.HEDGE if value else PositionMode.ONE_WAY, "exchange read-back", raw)
            if str(value).strip().lower() in {"true", "1", "hedge", "hedged", "dual", "dual_side"}:
                return ModeResult(PositionMode.HEDGE, "exchange read-back", raw)
            if str(value).strip().lower() in {"false", "0", "one_way", "one-way", "both"}:
                return ModeResult(PositionMode.ONE_WAY, "exchange read-back", raw)
        return ModeResult(PositionMode.UNKNOWN, "exchange response did not identify mode", raw)


def build_position_order_params(mode: PositionMode, side: str, *, opening: bool = True,
                                closing: bool = False, reduce_only_required: bool = False) -> Dict[str, Any]:
    """Build the sole supported position-side mapping for entry or exit."""
    mode = PositionMode(mode)
    side = str(side or "").upper()
    if mode is PositionMode.UNKNOWN:
        raise ValueError("position mode UNKNOWN; order blocked")
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"invalid order side: {side}")
    if opening == closing:
        raise ValueError("exactly one of opening or closing must be true")
    params: Dict[str, Any] = {"positionSide": "BOTH"}
    if mode is PositionMode.ONE_WAY:
        params["reduceOnly"] = bool(reduce_only_required or closing)
    else:
        params["positionSide"] = "LONG" if ((side == "BUY") == opening) else "SHORT"
        # BingX Hedge orders must not inherit One-Way reduceOnly semantics.
    return params


def leverage_params(mode: PositionMode, position_side: str) -> Dict[str, str]:
    if PositionMode(mode) is PositionMode.UNKNOWN:
        raise ValueError("position mode UNKNOWN; leverage blocked")
    if PositionMode(mode) is PositionMode.ONE_WAY:
        return {"side": "both"}
    return {"side": "long" if str(position_side).upper() == "LONG" else "short"}


class TradeExecutionService:
    """Single order gateway with no blind MARKET retries and fail-closed verification."""

    def __init__(self, exchange: Any, journal: Any = None, logger: Optional[Callable[[str], None]] = None,
                 api_call: Optional[Callable[..., Any]] = None):
        self.exchange = exchange
        self.journal = journal
        self.logger = logger or (lambda _message: None)
        self.api_call = api_call
        self.mode_adapter = BingXPositionModeAdapter(exchange, logger=self.logger, api_call=api_call)

    def _read(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self.api_call(method, *args, **kwargs) if self.api_call else method(*args, **kwargs)

    def _persist_pending(self, **fields: Any) -> None:
        if self.journal is not None and hasattr(self.journal, "persist_entry_pending"):
            self.journal.persist_entry_pending(**fields)

    def _find_order(self, symbol: str, order_id: Optional[str], client_order_id: str) -> Any:
        """Read back an existing order by exchange id, then client id; never creates an order."""
        fetch = getattr(self.exchange, "fetch_order", None)
        lookup_ids = [value for value in (order_id, client_order_id) if value]
        if callable(fetch):
            for candidate in lookup_ids:
                try:
                    try:
                        found = self._read(fetch, candidate, symbol, {"clientOrderId": client_order_id})
                    except TypeError:
                        found = self._read(fetch, candidate, symbol)
                    if isinstance(found, dict) and (_order_id(found) or found.get("clientOrderId")):
                        return found
                except Exception:
                    continue

        # Some adapters cannot look up by clientOrderId through fetch_order.
        # An open-order query is a read-only fallback; it is never an excuse to
        # re-submit the original MARKET order.
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        if callable(fetch_open) and client_order_id:
            try:
                rows = self._read(fetch_open, symbol)
            except TypeError:
                try:
                    rows = self._read(fetch_open, symbol, None, None, {"clientOrderId": client_order_id})
                except Exception:
                    rows = None
            except Exception:
                rows = None
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                info = _nested_info(row)
                client = row.get("clientOrderId") or info.get("clientOrderId") or info.get("clientOrderID")
                if str(client or "") == str(client_order_id):
                    return row
        return None

    @staticmethod
    def _symbol_key(value: Any) -> str:
        return str(value or "").upper().replace(":USDT", "").replace("/", "").replace("-", "").replace("_", "")

    def _position(self, symbol: str, position_side: str, expected_side: str):
        """Return (OPEN|FLAT|UNKNOWN|OTHER_SIDE, row, reason).

        An unavailable or malformed positions response is UNKNOWN, never FLAT.
        """
        fetch = getattr(self.exchange, "fetch_positions", None)
        if not callable(fetch):
            return "UNKNOWN", None, "fetch_positions unavailable"
        try:
            try:
                rows = self._read(fetch, [symbol])
            except TypeError:
                rows = self._read(fetch, [symbol], {})
        except Exception as exc:
            return "UNKNOWN", None, f"fetch_positions failed: {type(exc).__name__}: {exc}"
        if not isinstance(rows, (list, tuple)):
            return "UNKNOWN", None, "fetch_positions returned a non-list response"

        requested_key = self._symbol_key(symbol)
        target_rows = []
        ambiguous_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = _nested_info(row)
            raw_symbol = row.get("symbol") or info.get("symbol")
            if not raw_symbol:
                # A non-zero row without identity cannot safely be ignored.
                raw_qty = _position_quantity(row)
                if raw_qty > 0:
                    ambiguous_rows.append(row)
                continue
            if self._symbol_key(raw_symbol) != requested_key:
                continue
            qty = _position_quantity(row)
            if qty > 0:
                target_rows.append(row)

        if ambiguous_rows:
            return "UNKNOWN", None, "an open position row had no verifiable symbol"
        if not target_rows:
            return "FLAT", None, "no open position for requested symbol"

        expected = str(expected_side or "").upper()
        if expected not in {"BUY", "SELL"}:
            return "UNKNOWN", None, f"invalid expected side: {expected!r}"
        expected_direction = "LONG" if expected == "BUY" else "SHORT"
        matching = []
        opposite = []

        def signed_amount(row: Dict[str, Any]) -> Optional[float]:
            info = _nested_info(row)
            for value in (row.get("positionAmt"), info.get("positionAmt"), info.get("positionSize")):
                try:
                    number = float(value)
                    if math.isfinite(number) and abs(number) > 0:
                        return number
                except (TypeError, ValueError, OverflowError):
                    continue
            return None

        for row in target_rows:
            info = _nested_info(row)
            raw_position_side = str(row.get("positionSide") or row.get("posSide")
                                    or info.get("positionSide") or info.get("posSide") or "").upper()
            raw_side = str(row.get("side") or info.get("side") or "").upper()
            signed = signed_amount(row)
            direction = raw_position_side if raw_position_side in {"LONG", "SHORT"} else (
                raw_side if raw_side in {"LONG", "SHORT"} else
                ("LONG" if signed > 0 else "SHORT" if signed < 0 else
                 "LONG" if raw_side == "BUY" else "SHORT" if raw_side == "SELL" else "")
            )
            if not direction:
                return "UNKNOWN", None, "open position direction cannot be verified"
            if position_side in {"LONG", "SHORT"} and raw_position_side in {"LONG", "SHORT"}:
                if raw_position_side != position_side:
                    opposite.append(row)
                    continue
            if direction == expected_direction:
                matching.append(row)
            else:
                opposite.append(row)

        if len(matching) == 1 and not opposite:
            return "OPEN", matching[0], "matching exchange position confirmed"
        if matching or opposite:
            if not matching and len(opposite) == 1:
                return "OTHER_SIDE", opposite[0], f"exchange position side is not {expected_direction}"
            return "UNKNOWN", None, "multiple or conflicting position-side rows returned"
        return "UNKNOWN", None, "position rows could not be reconciled"

    def submit_market_entry(self, *, symbol: str, side: str, quantity: float, leverage: int = 20,
                            structural_stop: Optional[float] = None, trade_id: Optional[str] = None,
                            client_order_id: Optional[str] = None,
                            management_state: Optional[Dict[str, Any]] = None) -> ExecutionResult:
        side = str(side or "").upper()
        trade_id = str(trade_id or uuid.uuid4().hex)
        client_order_id = str(client_order_id or f"GGP-{trade_id[:20]}")[:32]
        try:
            requested = float(quantity)
        except (TypeError, ValueError, OverflowError):
            requested = 0.0
        if side not in {"BUY", "SELL"} or not math.isfinite(requested) or requested <= 0:
            return ExecutionResult(ExecutionState.REJECTED, trade_id, client_order_id,
                                   requested_qty=max(0.0, requested) if math.isfinite(requested) else 0.0,
                                   reason="entry not submitted: invalid side or quantity")

        mode_result = self.mode_adapter.read(symbol)
        if mode_result.mode is PositionMode.UNKNOWN:
            # No market-order request has been sent at this point, so this is a
            # definitive application-side block, not an ambiguous exchange write.
            return ExecutionResult(ExecutionState.REJECTED, trade_id, client_order_id,
                                   requested_qty=requested, position_mode=PositionMode.UNKNOWN.value,
                                   reason=f"entry not submitted: {mode_result.reason}")

        position_side = "LONG" if side == "BUY" else "SHORT"
        params = build_position_order_params(mode_result.mode, side, opening=True, closing=False)

        # Preserve the existing 20x account configuration path. The write is
        # single-shot; only the read-back is routed through the shared API policy.
        try:
            if not callable(getattr(self.exchange, "set_leverage", None)):
                raise RuntimeError("set_leverage unavailable")
            lev_params = leverage_params(mode_result.mode, position_side)
            self.exchange.set_leverage(int(leverage), symbol, lev_params)
            fetch_lev = getattr(self.exchange, "fetch_leverage", None)
            if not callable(fetch_lev):
                raise RuntimeError("fetch_leverage unavailable; leverage cannot be verified")
            try:
                lev_raw = self._read(fetch_lev, symbol, lev_params)
            except TypeError:
                lev_raw = self._read(fetch_lev, symbol)
            observed = extract_leverage_value(
                lev_raw,
                position_side=(position_side if mode_result.mode is PositionMode.HEDGE else None),
            )
            if observed is None or abs(observed - float(leverage)) > 1e-9:
                raise RuntimeError(f"leverage read-back mismatch: {observed!r}")
        except Exception as exc:
            return ExecutionResult(ExecutionState.REJECTED, trade_id, client_order_id,
                                   requested_qty=requested, position_mode=mode_result.mode.value,
                                   position_side=params["positionSide"],
                                   reason=f"leverage validation failed: {type(exc).__name__}: {exc}")

        params["clientOrderId"] = client_order_id
        try:
            self._persist_pending(trade_id=trade_id, client_order_id=client_order_id, symbol=symbol,
                                  side=side, position_mode=mode_result.mode.value,
                                  position_side=params["positionSide"], leverage=leverage,
                                  requested_qty=requested, entry_ts=time.time(), status="ENTRY_PENDING",
                                  structural_stop=structural_stop, management_state=management_state)
        except Exception as exc:
            return ExecutionResult(ExecutionState.REJECTED, trade_id, client_order_id,
                                   requested_qty=requested, position_mode=mode_result.mode.value,
                                   position_side=params["positionSide"],
                                   reason=f"entry not submitted: ENTRY_PENDING journal write failed: {type(exc).__name__}: {exc}")

        # A MARKET submission is non-idempotent. Exactly one create_order call is
        # allowed for this attempt; any timeout is reconciled by clientOrderId.
        try:
            order = self.exchange.create_order(symbol, "market", side.lower(), requested, None, params)
        except Exception as exc:
            reconciled = self._find_order(symbol, None, client_order_id)
            if reconciled is None:
                return ExecutionResult(ExecutionState.EXECUTION_UNCERTAIN, trade_id, client_order_id,
                                       requested_qty=requested, position_mode=mode_result.mode.value,
                                       position_side=params["positionSide"],
                                       reason=f"submission outcome uncertain; order read-back unavailable: {type(exc).__name__}: {exc}",
                                       submission_attempted=True)
            order = reconciled

        order_id = _order_id(order)
        observed_order = self._find_order(symbol, order_id, client_order_id) or order
        filled = _extract_filled(observed_order)
        remaining = _extract_remaining(observed_order, requested, filled)
        average = _extract_average(observed_order)
        if average is not None and average <= 0:
            average = None
        order_status = _status(observed_order)
        order_terminal = order_status in _TERMINAL_ENTRY_ORDER_STATES

        # If BingX still reports a live remainder, cancel that same remainder only.
        # The cancel acknowledgement is not authority; read the original order back.
        if not order_terminal:
            cancel = getattr(self.exchange, "cancel_order", None)
            cancel_id = order_id or client_order_id
            if callable(cancel) and cancel_id:
                try:
                    cancel(cancel_id, symbol)
                except Exception:
                    pass
                refreshed = self._find_order(symbol, order_id, client_order_id)
                if refreshed is not None:
                    observed_order = refreshed
                    order_status = _status(observed_order)
                    order_terminal = order_status in _TERMINAL_ENTRY_ORDER_STATES
                    filled = _extract_filled(observed_order)
                    remaining = _extract_remaining(observed_order, requested, filled)
                    average = _extract_average(observed_order)
                    if average is not None and average <= 0:
                        average = None

        position_status, position, position_reason = self._position(
            symbol, params["positionSide"], side)
        if average is None and filled > 0:
            trades = []
            fetch_trades = getattr(self.exchange, "fetch_my_trades", None)
            if callable(fetch_trades):
                try:
                    trades = self._read(fetch_trades, symbol, None, 100, {"orderId": order_id})
                except Exception:
                    pass
            average = _weighted_trade_average(trades)
        if average is None and filled > 0 and position is not None:
            average = _position_entry_price(position)
            if average is not None and average <= 0:
                average = None

        reason = ""
        if not order_terminal:
            state = ExecutionState.EXECUTION_UNCERTAIN
            reason = f"entry order is not terminal after cancel/read-back; status={order_status}"
        elif filled <= 0:
            if position_status == "FLAT":
                state = ExecutionState.REJECTED
                reason = f"terminal zero-fill order confirmed FLAT; status={order_status}"
            else:
                state = ExecutionState.EXECUTION_UNCERTAIN
                reason = f"terminal zero-fill order conflicts with position state={position_status}: {position_reason}"
        elif position_status == "OTHER_SIDE":
            state = ExecutionState.EXECUTION_UNCERTAIN
            reason = f"exchange position side mismatch for requested {side}: {position_reason}"
        elif position_status != "OPEN" or position is None:
            state = ExecutionState.EXECUTION_UNCERTAIN
            reason = f"exchange position read-back {position_status}: {position_reason}"
        else:
            position_qty = _position_quantity(position)
            quantity_tolerance = max(requested * 0.002, 1e-10)
            if position_qty <= 0:
                state = ExecutionState.EXECUTION_UNCERTAIN
                reason = "exchange position quantity is unavailable"
            elif abs(position_qty - filled) > quantity_tolerance:
                state = ExecutionState.EXECUTION_UNCERTAIN
                reason = (
                    f"order/position quantity mismatch: order_filled={filled:.12g} "
                    f"position_qty={position_qty:.12g}"
                )
            elif average is None:
                state = ExecutionState.ENTRY_PRICE_UNKNOWN
                reason = f"order_status={order_status}; exchange entry price unavailable"
            elif filled + quantity_tolerance < requested or order_status in {"CANCELED", "CANCELLED", "EXPIRED"}:
                state = ExecutionState.PARTIAL_FILL
                reason = f"order_status={order_status}; terminal partial fill and exchange position confirmed"
            elif remaining > quantity_tolerance and order_status not in {"CLOSED", "FILLED"}:
                state = ExecutionState.EXECUTION_UNCERTAIN
                reason = f"terminal order has an unresolved remainder; status={order_status}"
            else:
                state = ExecutionState.FILLED
                reason = f"order_status={order_status}; exchange position confirmed"

        result = ExecutionResult(
            state, trade_id, client_order_id, order_id, requested, filled, remaining,
            average, average, mode_result.mode.value, params["positionSide"],
            reason=reason, raw_order=observed_order, raw_position=position,
            order_terminal=order_terminal, submission_attempted=True,
            position_status=position_status,
        )
        if self.journal is not None and hasattr(self.journal, "record_execution"):
            try:
                self.journal.record_execution(
                    trade_id, exchange_order_id=order_id, filled_qty=filled,
                    remaining_qty=remaining, average_fill_price=average,
                    actual_entry_price=average, status=state.value,
                    recovery_status=("EXECUTION_UNCERTAIN" if state in {
                        ExecutionState.EXECUTION_UNCERTAIN,
                        ExecutionState.ENTRY_PRICE_UNKNOWN,
                    } else ""),
                )
            except Exception as exc:
                # The exchange may already represent exposure. A failed durable
                # checkpoint must never unlock or make this attempt retryable.
                return ExecutionResult(
                    ExecutionState.EXECUTION_UNCERTAIN, trade_id, client_order_id,
                    order_id, requested, filled, remaining, average, average,
                    mode_result.mode.value, params["positionSide"],
                    reason=f"execution journal persistence failed after exchange response: {type(exc).__name__}: {exc}",
                    raw_order=observed_order, raw_position=position,
                    order_terminal=order_terminal, submission_attempted=True,
                    position_status=position_status,
                )
        return result

    def place_and_verify_native_stop(self, *, symbol: str, side: str, quantity: float,
                                     trigger_price: float, mode: PositionMode,
                                     position_side: str) -> Optional[str]:
        close_side = "sell" if str(side).upper() == "BUY" else "buy"
        params = build_position_order_params(mode, "SELL" if close_side == "sell" else "BUY",
                                             opening=False, closing=True,
                                             reduce_only_required=(mode is PositionMode.ONE_WAY))
        params.update({"stopPrice": float(trigger_price), "workingType": "MARK_PRICE"})
        submitted_id = None
        try:
            # Non-idempotent protection write: send once. On timeout, read back
            # the exact expected stop before deciding that protection is absent.
            order = self.exchange.create_order(symbol, "STOP_MARKET", close_side, float(quantity), None, params)
            submitted_id = _order_id(order)
        except Exception as exc:
            self.logger(f"native stop submission response ambiguous for {symbol}: {type(exc).__name__}: {exc}; read-back only")
        verified = self._read_back_native_stop(
            symbol=symbol, close_side=close_side, position_side=params["positionSide"],
            trigger_price=float(trigger_price), quantity=float(quantity))
        if verified:
            return verified
        self.logger(f"native stop could not be verified for {symbol}; submitted_order_id={submitted_id!r}")
        return None

    def _read_back_native_stop(self, *, symbol: str, close_side: str, position_side: str,
                               trigger_price: float, quantity: float) -> Optional[str]:
        """Return the order id of an active native stop matching all protection fields."""
        from bingx_protection import extract_open_order_records, is_matching_native_stop_order, native_order_identifiers
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        try:
            market = self.exchange.market(symbol)
            market_id = market.get("id") or symbol
        except Exception:
            market_id = symbol
        for attempt, delay in enumerate((0.0, 0.25, 0.50), start=1):
            if delay:
                time.sleep(delay)
            rows = []
            if callable(fetch_open):
                for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                    try:
                        candidate = self._read(fetch_open, *args)
                        if isinstance(candidate, list):
                            rows.extend(candidate)
                    except TypeError:
                        continue
                    except Exception as exc:
                        self.logger(f"native stop read-back attempt={attempt} failed: {type(exc).__name__}: {exc}")
            raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
            if callable(raw_method):
                for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                    try:
                        raw = self._read(raw_method, {"symbol": market_id, "type": native_type})
                        rows.extend(extract_open_order_records(raw))
                    except Exception as exc:
                        self.logger(f"native stop {native_type} read-back attempt={attempt} failed: {type(exc).__name__}: {exc}")
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_ids = native_order_identifiers(row)
                if row_ids.intersection(seen):
                    continue
                seen.update(row_ids)
                if is_matching_native_stop_order(
                    row, symbol=symbol, position_side=position_side, close_side=close_side.upper(),
                    trigger_price=float(trigger_price), required_qty=float(quantity)):
                    ordered = [str(x) for x in (row.get("id"), row.get("orderId"),
                                                row.get("triggerOrderId")) if x not in (None, "")]
                    return ordered[0] if ordered else (next(iter(row_ids)) if row_ids else None)
        return None

    def _active_stop_id_at(self, *, symbol: str, close_side: str, position_side: str,
                           trigger_price: float, quantity: float) -> Optional[str]:
        """Return the id of any active native stop matching the given trigger/qty."""
        return self._read_back_native_stop(symbol=symbol, close_side=close_side,
                                           position_side=position_side,
                                           trigger_price=float(trigger_price),
                                           quantity=float(quantity))

    def _any_active_stop_id(self, *, symbol: str, close_side: str, position_side: str) -> Optional[str]:
        """Return the id of any active conditional stop for the scope, price-agnostic.

        Used only to avoid placing a duplicate stop; it never treats a plain MARKET
        order as protection.
        """
        from bingx_protection import extract_open_order_records, native_order_identifiers
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        try:
            market_id = (self.exchange.market(symbol) or {}).get("id") or symbol
        except Exception:
            market_id = symbol
        rows = []
        if callable(fetch_open):
            for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                try:
                    candidate = self._read(fetch_open, *args)
                    if isinstance(candidate, list):
                        rows.extend(candidate)
                except Exception:
                    continue
        raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
        if callable(raw_method):
            for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                try:
                    rows.extend(extract_open_order_records(self._read(raw_method, {"symbol": market_id, "type": native_type})))
                except Exception:
                    continue
        want_symbol = str(symbol).upper().replace("/", "-").split(":")[0]
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            type_values = {str(v).upper() for v in (row.get("type"), info.get("type"),
                                                    row.get("orderType"), info.get("origType")) if v not in (None, "")}
            if not (type_values & {"STOP_MARKET", "TRIGGER_MARKET"}):
                continue
            raw_symbol = str(row.get("symbol") or info.get("symbol") or "").upper().replace("/", "-").split(":")[0]
            if raw_symbol and raw_symbol != want_symbol:
                continue
            side = str(row.get("side") or info.get("side") or "").upper()
            if side and side != str(close_side).upper():
                continue
            pos_side = str(row.get("positionSide") or info.get("positionSide") or "").upper()
            if pos_side and pos_side != str(position_side).upper():
                continue
            status = str(row.get("status") or info.get("status") or row.get("state") or "").upper()
            if status not in {"NEW", "OPEN", "PENDING", "PARTIALLY_FILLED", "PARTIALLYFILLED", "WORKING"}:
                continue
            ids = native_order_identifiers(row)
            for key in ("id", "orderId", "triggerOrderId"):
                value = row.get(key) or info.get(key)
                if value not in (None, ""):
                    return str(value)
            if ids:
                return next(iter(ids))
        return None

    def _verified_retained(self, *, symbol: str, close_side: str, position_side: str,
                           trigger_price: Optional[float], quantity: float) -> Optional[str]:
        """Return an id only when the previous stop is provably active on a fresh read.

        Never infers retention from an API acknowledgement, local state, or an
        ambiguous cancel result. When the previous trigger price is unknown, any
        active stop for the scope must still cover the required quantity.
        """
        if trigger_price and trigger_price > 0:
            return self._active_stop_id_at(symbol=symbol, close_side=close_side,
                                           position_side=position_side,
                                           trigger_price=float(trigger_price),
                                           quantity=float(quantity))
        # Unknown previous price: require an active stop that covers the quantity.
        from bingx_protection import extract_open_order_records, is_matching_native_stop_order, native_order_identifiers
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        try:
            market_id = (self.exchange.market(symbol) or {}).get("id") or symbol
        except Exception:
            market_id = symbol
        rows = []
        if callable(fetch_open):
            for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                try:
                    candidate = self._read(fetch_open, *args)
                    if isinstance(candidate, list):
                        rows.extend(candidate)
                except Exception:
                    continue
        raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
        if callable(raw_method):
            for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                try:
                    rows.extend(extract_open_order_records(self._read(raw_method, {"symbol": market_id, "type": native_type})))
                except Exception:
                    continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            ids = native_order_identifiers(row)
            if ids and ids.issubset(seen):
                continue
            seen.update(ids)
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            type_values = {str(v).upper() for v in (row.get("type"), info.get("type"),
                                                    row.get("orderType"), info.get("origType")) if v not in (None, "")}
            if not (type_values & {"STOP_MARKET", "TRIGGER_MARKET"}):
                continue
            raw_symbol = str(row.get("symbol") or info.get("symbol") or "").upper().replace("/", "-").split(":")[0]
            want_symbol = str(symbol).upper().replace("/", "-").split(":")[0]
            if raw_symbol and raw_symbol != want_symbol:
                continue
            side = str(row.get("side") or info.get("side") or "").upper()
            if side and side != str(close_side).upper():
                continue
            pos_side = str(row.get("positionSide") or info.get("positionSide") or "").upper()
            if pos_side and pos_side != str(position_side).upper():
                continue
            status = str(row.get("status") or info.get("status") or row.get("state") or "").upper()
            if status not in {"NEW", "OPEN", "PENDING", "PARTIALLY_FILLED", "PARTIALLYFILLED", "WORKING"}:
                continue
            for key in ("id", "orderId", "triggerOrderId"):
                value = row.get(key) or info.get(key)
                if value not in (None, ""):
                    return str(value)
            if ids:
                return next(iter(ids))
        return None

    def _read_open_stop_rows(self, symbol: str):
        """Return (rows, ok). ok is False when no open-order query path succeeded.

        Callers must treat ok=False as UNKNOWN (not as "no stops"). A failed read
        must never be mistaken for proof that an order is gone.
        """
        from bingx_protection import extract_open_order_records
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        try:
            market_id = (self.exchange.market(symbol) or {}).get("id") or symbol
        except Exception:
            market_id = symbol
        rows = []
        ok = False
        if callable(fetch_open):
            for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                try:
                    candidate = self._read(fetch_open, *args)
                    if isinstance(candidate, list):
                        rows.extend(candidate)
                        ok = True
                except TypeError:
                    continue
                except Exception:
                    continue
        raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
        if callable(raw_method):
            for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                try:
                    rows.extend(extract_open_order_records(self._read(raw_method, {"symbol": market_id, "type": native_type})))
                    ok = True
                except Exception:
                    continue
        return rows, ok

    def _stop_id_presence(self, order_id: str, *, symbol: str, close_side: str,
                          position_side: str) -> str:
        """Return "ACTIVE", "GONE", or "UNKNOWN" for one exact order id.

        "GONE" is only returned after a successful open-orders read that does not
        contain the id. A failed read returns "UNKNOWN" and must never be treated
        as proof of cancellation.
        """
        from bingx_protection import native_order_identifiers
        want = str(order_id or "")
        if not want:
            return "UNKNOWN"
        rows, ok = self._read_open_stop_rows(symbol)
        if not ok:
            return "UNKNOWN"
        want_symbol = str(symbol).upper().replace("/", "-").split(":")[0]
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            type_values = {str(v).upper() for v in (row.get("type"), info.get("type"),
                                                    row.get("orderType"), info.get("origType")) if v not in (None, "")}
            if not (type_values & {"STOP_MARKET", "TRIGGER_MARKET"}):
                continue
            status = str(row.get("status") or info.get("status") or row.get("state") or "").upper()
            if status not in {"NEW", "OPEN", "PENDING", "PARTIALLY_FILLED", "PARTIALLYFILLED", "WORKING"}:
                continue
            raw_symbol = str(row.get("symbol") or info.get("symbol") or "").upper().replace("/", "-").split(":")[0]
            if raw_symbol and raw_symbol != want_symbol:
                continue
            side = str(row.get("side") or info.get("side") or "").upper()
            if side and side != str(close_side).upper():
                continue
            pos_side = str(row.get("positionSide") or info.get("positionSide") or "").upper()
            if pos_side and pos_side != str(position_side).upper():
                continue
            if want in native_order_identifiers(row):
                return "ACTIVE"
        return "GONE"

    def _is_stop_id_active(self, order_id: str, *, symbol: str, close_side: str,
                           position_side: str) -> bool:
        """Return True only if this exact order id is proven still active.

        Uses the tri-state presence check, so an unreadable state returns False
        (not active) rather than a false "gone" claim.
        """
        return self._stop_id_presence(order_id, symbol=symbol, close_side=close_side,
                                      position_side=position_side) == "ACTIVE"

    def _precheck_native_stop(self, *, symbol: str, side: str, close_side: str, position_side: str,
                              mode: PositionMode, quantity: float, new_trigger_price: float,
                              previous_order_id: Optional[str], previous_trigger_price: Optional[float]):
        """Validate everything before any destructive action. Returns (ok, reason).

        Reads the live position and open stops from BingX, verifies the position
        identity and quantity, verifies the old stop when one is expected, and
        validates that the new trigger price is on the correct side of the market
        for the position direction. Any uncertainty aborts the replacement.
        """
        if quantity <= 0:
            return False, "precheck_invalid_quantity"
        if new_trigger_price <= 0:
            return False, "precheck_invalid_new_trigger_price"
        # Position identity must be provable from the exchange.
        try:
            rows = self._read(self.exchange.fetch_positions, [symbol])
        except Exception as exc:
            return False, f"precheck_position_read_failed:{type(exc).__name__}"
        if not isinstance(rows, list):
            return False, "precheck_position_read_invalid"
        expected_direction = "LONG" if str(side).upper() == "BUY" else "SHORT"
        matched = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = _nested_info(row)
            raw_pos_side = str(row.get("positionSide") or row.get("posSide")
                               or info.get("positionSide") or info.get("posSide") or "").upper()
            raw_side = str(row.get("side") or info.get("side") or "").upper()
            direction = raw_pos_side if raw_pos_side in {"LONG", "SHORT"} else (
                raw_side if raw_side in {"LONG", "SHORT"} else "")
            contracts = _first_number(row.get("contracts"))
            if contracts is None:
                contracts = _first_number(info.get("positionAmt"))
                contracts = abs(contracts) if contracts is not None else None
            if direction and direction != expected_direction:
                continue
            if mode is PositionMode.HEDGE:
                # Hedge requires an explicit positionSide on the position record.
                if raw_pos_side not in {"LONG", "SHORT"}:
                    return False, "precheck_hedge_position_side_missing"
                if raw_pos_side != position_side:
                    continue
            matched = {"contracts": contracts or 0.0, "direction": direction}
            break
        if matched is None:
            return False, "precheck_position_identity_unproven"
        if matched["contracts"] <= 0:
            return False, "precheck_position_quantity_not_positive"
        if quantity > matched["contracts"] * 1.005:
            return False, "precheck_requested_qty_exceeds_position"
        # Market price for directional sanity of the new stop.
        mark = 0.0
        try:
            ticker = self._read(self.exchange.fetch_ticker, symbol)
            if isinstance(ticker, dict):
                mark = _first_number(ticker.get("last"), ticker.get("close")) or 0.0
        except Exception:
            mark = 0.0
        if mark > 0:
            if str(side).upper() == "BUY" and new_trigger_price >= mark:
                return False, "precheck_stop_wrong_side_of_market_for_long"
            if str(side).upper() == "SELL" and new_trigger_price <= mark:
                return False, "precheck_stop_wrong_side_of_market_for_short"
        # The previous stop, when one is expected, must be provable.
        if previous_order_id:
            if previous_trigger_price and previous_trigger_price > 0:
                still = self._active_stop_id_at(symbol=symbol, close_side=close_side,
                                                position_side=position_side,
                                                trigger_price=float(previous_trigger_price),
                                                quantity=float(quantity))
                if not still:
                    return False, "precheck_previous_stop_not_found"
            else:
                any_stop = self._any_active_stop_id(symbol=symbol, close_side=close_side,
                                                    position_side=position_side)
                if not any_stop:
                    return False, "precheck_previous_stop_not_found"
        return True, "ok"

    def _count_matching_stops(self, *, symbol: str, close_side: str, position_side: str,
                              trigger_price: float, quantity: float) -> int:
        """Count active stops matching the full protection scope (conflict detection)."""
        from bingx_protection import extract_open_order_records, is_matching_native_stop_order
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        try:
            market_id = (self.exchange.market(symbol) or {}).get("id") or symbol
        except Exception:
            market_id = symbol
        rows = []
        if callable(fetch_open):
            for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                try:
                    candidate = self._read(fetch_open, *args)
                    if isinstance(candidate, list):
                        rows.extend(candidate)
                except Exception:
                    continue
        raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
        if callable(raw_method):
            for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                try:
                    rows.extend(extract_open_order_records(self._read(raw_method, {"symbol": market_id, "type": native_type})))
                except Exception:
                    continue
        count = 0
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            from bingx_protection import native_order_identifiers
            ids = native_order_identifiers(row)
            if ids and ids.issubset(seen):
                continue
            seen.update(ids)
            if is_matching_native_stop_order(
                row, symbol=symbol, position_side=position_side, close_side=close_side.upper(),
                trigger_price=float(trigger_price), required_qty=float(quantity)):
                count += 1
        return count

    def replace_native_stop(self, *, symbol: str, side: str, quantity: float,
                            new_trigger_price: float, mode: PositionMode, position_side: str,
                            previous_order_id: Optional[str] = None,
                            previous_trigger_price: Optional[float] = None,
                            previous_quantity: Optional[float] = None) -> "StopMoveResult":
        """Move an existing native stop as an explicit PRE/REPLACE/POST state machine.

        Safety contract: the position must never be left without a verified stop,
        and no state is adopted from a stale API acknowledgement. Outcomes:
          - MOVED:    the new stop is verified active and matching on a fresh read.
          - RETAINED: the previous stop is verified active and covering on a fresh
                      read (never inferred from a cancel ambiguity or local state).
          - LOST:     no stop can be proven active at any level.
        """
        close_side = "sell" if str(side).upper() == "BUY" else "buy"
        params = build_position_order_params(mode, "SELL" if close_side == "sell" else "BUY",
                                             opening=False, closing=True,
                                             reduce_only_required=(mode is PositionMode.ONE_WAY))
        params.update({"stopPrice": float(new_trigger_price), "workingType": "MARK_PRICE"})
        try:
            market_id = (self.exchange.market(symbol) or {}).get("id") or symbol
        except Exception:
            market_id = symbol

        # -------- PRECHECK --------
        ok, reason = self._precheck_native_stop(
            symbol=symbol, side=side, close_side=close_side, position_side=position_side,
            mode=mode, quantity=float(quantity), new_trigger_price=float(new_trigger_price),
            previous_order_id=previous_order_id, previous_trigger_price=previous_trigger_price)
        if not ok:
            self.logger(f"native stop replace aborted at PRECHECK: {reason}")
            # Do not touch anything. Report RETAINED only if a verified stop is
            # provably active; otherwise LOST so the caller holds.
            retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                               position_side=position_side,
                                               trigger_price=previous_trigger_price,
                                               quantity=float(quantity))
            if retained:
                return StopMoveResult("RETAINED", retained, reason)
            return StopMoveResult("LOST", None, reason)

        def verified_at(trigger: float, qty: float) -> Optional[str]:
            """Fresh read: return the id of an active stop matching price and coverage."""
            return self._active_stop_id_at(symbol=symbol, close_side=close_side,
                                           position_side=position_side,
                                           trigger_price=float(trigger), quantity=float(qty))

        def place_new(trigger: float, qty: float) -> Optional[str]:
            """Place a stop and verify it; never adopt a mismatched existing stop.

            If a non-matching stop is active it must be cancelled AND its removal
            proven by a fresh read before any replacement is submitted. An
            unproven cancel (exception, unreadable state, or still-active id)
            aborts without creating. A failed open-orders read is treated as
            UNKNOWN, never as "no stop exists".
            """
            existing_exact = verified_at(trigger, qty)
            if existing_exact:
                return existing_exact
            # Only proceed when the current open-order state is readable; otherwise
            # creating could duplicate an unreadable existing stop.
            _rows, ok = self._read_open_stop_rows(symbol)
            if not ok:
                self.logger("open-orders read failed; refusing to create a stop on an unproven state")
                return None
            stale = self._any_active_stop_id(symbol=symbol, close_side=close_side,
                                             position_side=position_side)
            if stale:
                cancel = getattr(self.exchange, "cancel_order", None)
                if not callable(cancel):
                    self.logger(f"a non-matching stop {stale} is active and cannot be cancelled; not placing")
                    return None
                try:
                    cancel(str(stale), symbol)
                except Exception as exc:
                    self.logger(f"could not cancel non-matching stop {stale}: {type(exc).__name__}: {exc}")
                    return None
                # Verify the old id actually disappeared before creating a new one.
                # A successful cancel call is not proof of cancellation.
                if self._stop_id_presence(str(stale), symbol=symbol, close_side=close_side,
                                          position_side=position_side) != "GONE":
                    self.logger(f"cancel of stale stop {stale} not proven gone; refusing to create a replacement")
                    return None
            try:
                stop_params = dict(params)
                stop_params["stopPrice"] = float(trigger)
                self.exchange.create_order(symbol, "STOP_MARKET", close_side, float(qty), None, stop_params)
            except Exception as exc:
                self.logger(f"native stop place failed at {trigger}: {type(exc).__name__}: {exc}")
                return None
            return verified_at(trigger, qty)

        def restore_previous() -> Optional[str]:
            """Re-establish the previous stop and verify coverage on a fresh read."""
            if previous_trigger_price and previous_trigger_price > 0:
                still = verified_at(float(previous_trigger_price), float(quantity))
                if still:
                    return still
                return place_new(float(previous_trigger_price), float(quantity))
            return None

        # -------- REPLACEMENT --------
        cancel_replace = getattr(self.exchange, "swapV1PrivatePostTradeCancelReplace", None)
        if callable(cancel_replace) and previous_order_id:
            request = dict(params)
            request.update({
                "symbol": market_id,
                "side": "SELL" if close_side == "sell" else "BUY",
                "positionSide": params["positionSide"],
                "type": "STOP_MARKET",
                "quantity": self.exchange.amount_to_precision(symbol, float(quantity)),
                "cancelReplaceMode": "STOP_ON_FAILURE",
                "cancelRestrictions": "ONLY_NEW",
                "cancelOrderId": str(previous_order_id),
            })
            if mode is not PositionMode.ONE_WAY:
                request.pop("reduceOnly", None)
            cancel_replace_ambiguous = False
            cancel_replace_rejected = False
            try:
                response = cancel_replace(request)
                if isinstance(response, dict):
                    data = response.get("data") if isinstance(response.get("data"), dict) else {}
                    replace_result = str(data.get("replaceResult", "")).lower()
                    if replace_result not in {"", "true"}:
                        cancel_replace_rejected = True
                        self.logger(f"native stop cancelReplace reported failure: {data.get('replaceMsg')}")
            except Exception as exc:
                # Transport error: outcome unknown. Never assume; verify fresh.
                cancel_replace_ambiguous = True
                self.logger(f"native stop cancelReplace error: {type(exc).__name__}: {exc}")
            if cancel_replace_ambiguous:
                # The old stop may still be live. Never create a new stop on an
                # unproven state: verify the exact old id and the requested level,
                # otherwise hold.
                old_active = self._is_stop_id_active(str(previous_order_id), symbol=symbol,
                                                     close_side=close_side, position_side=position_side)
                if old_active:
                    retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                                       position_side=position_side,
                                                       trigger_price=previous_trigger_price,
                                                       quantity=float(quantity))
                    if retained:
                        return StopMoveResult("RETAINED", retained, "cancelreplace_ambiguous_old_stop_active")
                    return StopMoveResult("LOST", None, "cancelreplace_ambiguous_outcome_unproven")
                new_verified = verified_at(float(new_trigger_price), float(quantity))
                if new_verified:
                    return StopMoveResult("MOVED", new_verified)
                retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                                   position_side=position_side,
                                                   trigger_price=previous_trigger_price,
                                                   quantity=float(quantity))
                if retained:
                    return StopMoveResult("RETAINED", retained, "cancelreplace_ambiguous_previous_verified")
                return StopMoveResult("LOST", None, "cancelreplace_ambiguous_no_verified_stop")
            if cancel_replace_rejected:
                # Explicit rejection: STOP_ON_FAILURE leaves the old stop in place.
                retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                                   position_side=position_side,
                                                   trigger_price=previous_trigger_price,
                                                   quantity=float(quantity))
                if retained:
                    return StopMoveResult("RETAINED", retained, "cancelreplace_rejected_old_stop_retained")
                return StopMoveResult("LOST", None, "cancelreplace_rejected_no_verified_stop")
            return self._postcheck(symbol=symbol, close_side=close_side, position_side=position_side,
                                   new_trigger_price=float(new_trigger_price), quantity=float(quantity),
                                   verified_at=verified_at, restore_previous=restore_previous,
                                   place_new=place_new)

        if previous_order_id:
            # Cancel + place fallback. The cancel result must be verified before any
            # replacement is submitted; an ambiguous result never triggers creation.
            cancel = getattr(self.exchange, "cancel_order", None)
            if not callable(cancel):
                self.logger("native stop cancel is unavailable; refusing an unverified replacement")
                retained = restore_previous()
                return StopMoveResult("RETAINED", retained) if retained else StopMoveResult("LOST")
            cancel_ambiguous = False
            try:
                cancel(str(previous_order_id), symbol)
            except Exception as exc:
                cancel_ambiguous = True
                self.logger(f"native stop cancel failed for {previous_order_id}: {type(exc).__name__}: {exc}")
            # Verify the exact previous order id against a fresh read.
            still_active = self._is_stop_id_active(str(previous_order_id), symbol=symbol,
                                                  close_side=close_side, position_side=position_side)
            if cancel_ambiguous and still_active:
                # Ambiguous cancel and the old stop is still live: keep it, add nothing.
                retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                                   position_side=position_side,
                                                   trigger_price=previous_trigger_price,
                                                   quantity=float(quantity))
                if retained:
                    return StopMoveResult("RETAINED", retained, "cancel_ambiguous_old_stop_still_active")
                return StopMoveResult("LOST", None, "cancel_ambiguous_outcome_unproven")
            if still_active:
                # Cancel clearly did not take effect: keep existing protection.
                self.logger(f"cancel of {previous_order_id} not confirmed; keeping existing protection")
                retained = restore_previous()
                return StopMoveResult("RETAINED", retained) if retained else StopMoveResult("LOST")
            if cancel_ambiguous:
                # Ambiguous cancel and the old id is no longer active, but the outcome
                # is not proven (it could have been rejected while still live under a
                # different read). Do not create a new stop on an unproven state; hold
                # unless an active covering stop can be verified.
                retained = self._verified_retained(symbol=symbol, close_side=close_side,
                                                   position_side=position_side,
                                                   trigger_price=previous_trigger_price,
                                                   quantity=float(quantity))
                if retained:
                    return StopMoveResult("RETAINED", retained, "cancel_ambiguous_protection_verified")
                return StopMoveResult("LOST", None, "cancel_ambiguous_no_verified_stop")
            # Cancel confirmed gone; place and verify the new one.
            placed = place_new(float(new_trigger_price), float(quantity))
            if placed:
                return StopMoveResult("MOVED", placed)
            retained = restore_previous()
            return StopMoveResult("RETAINED", retained) if retained else StopMoveResult("LOST")

        # No previous stop recorded: place the new protection directly.
        placed = place_new(float(new_trigger_price), float(quantity))
        return StopMoveResult("MOVED", placed) if placed else StopMoveResult("LOST")

    def _postcheck(self, *, symbol: str, close_side: str, position_side: str, new_trigger_price: float,
                   quantity: float, verified_at, restore_previous, place_new) -> "StopMoveResult":
        """Confirm the new stop on a fresh read; otherwise re-establish the previous one."""
        new_verified = verified_at(float(new_trigger_price), float(quantity))
        if new_verified:
            conflicts = self._count_matching_stops(symbol=symbol, close_side=close_side,
                                                   position_side=position_side,
                                                   trigger_price=float(new_trigger_price),
                                                   quantity=float(quantity))
            if conflicts > 1:
                self.logger(f"native stop conflict: {conflicts} matching stops at the new level")
                return StopMoveResult("LOST", None, "postcheck_duplicate_stops")
            return StopMoveResult("MOVED", new_verified)
        # Not confirmed by the atomic path. Try placing the new stop, then verify.
        placed = place_new(float(new_trigger_price), float(quantity))
        if placed:
            return StopMoveResult("MOVED", placed)
        retained = restore_previous()
        if retained:
            return StopMoveResult("RETAINED", retained, "replacement_not_confirmed_previous_verified")
        return StopMoveResult("LOST", None, "no_verified_stop_after_replacement")
