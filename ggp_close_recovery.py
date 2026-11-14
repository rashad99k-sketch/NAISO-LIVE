"""Small, pure helpers for safe contract-amount recovery.

The trading runtime owns exchange reconciliation and order submission. This module
only interprets CCXT market precision and performs deterministic, downward-only
quantization; it never talks to an exchange or sends an order.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
from typing import Any, Optional


def amount_step_from_market(market: Any, precision_mode: Any) -> Optional[float]:
    """Return the amount step when CCXT market metadata defines it unambiguously."""
    if not isinstance(market, dict) or not market.get("contract"):
        return None
    precision = market.get("precision")
    if not isinstance(precision, dict):
        return None
    raw = precision.get("amount")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None

    # CCXT constants: DECIMAL_PLACES=2, SIGNIFICANT_DIGITS=3, TICK_SIZE=4.
    try:
        mode = int(precision_mode)
    except (TypeError, ValueError):
        return None
    if mode == 4:  # TICK_SIZE: precision.amount is the actual step.
        return float(value) if value > 0 else None
    if mode == 2:  # DECIMAL_PLACES: precision.amount is decimal places.
        places = int(value)
        if value != Decimal(places) or places < 0:
            return None
        return float(Decimal(1).scaleb(-places))
    # Significant digits are quantity-dependent; do not guess a global step.
    return None


def floor_amount_to_step(amount: Any, step: Any) -> float:
    """Quantize down only; never increase the requested exposure reduction."""
    try:
        value = Decimal(str(amount))
        increment = Decimal(str(step))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if not value.is_finite() or not increment.is_finite() or value <= 0 or increment <= 0:
        return 0.0
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    result = units * increment
    return float(result) if result > 0 else 0.0


def ceil_amount_to_step(amount: Any, step: Any) -> float:
    """Round a remaining-runner target upward so TP1 never closes over half."""
    try:
        value = Decimal(str(amount))
        increment = Decimal(str(step))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    if not value.is_finite() or not increment.is_finite() or value <= 0 or increment <= 0:
        return 0.0
    units = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    result = units * increment
    return float(result)


def is_contract_amount_precision_error(error: Any) -> bool:
    """Recognize only CCXT's explicit natural-number contract-amount rejection."""
    message = str(error or "").lower()
    return (
        "amount of " in message
        and "must be greater than minimum amount precision of 1" in message
    )



def recover_integer_contract_amount(requested: Any, before_qty: Any, observed_qty: Any,
                                    position_status: str) -> Optional[float]:
    """Return one smaller integer-contract retry only after unchanged OPEN exposure.

    None means the exchange position is not proven unchanged. Zero means there is
    no safe smaller positive integer amount to retry. A positive result is safe to
    use for exactly one retry after an explicit CCXT amount-precision rejection.
    """
    if str(position_status or "").upper() != "OPEN":
        return None
    try:
        before = Decimal(str(before_qty))
        observed = Decimal(str(observed_qty))
        amount = Decimal(str(requested))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not all(x.is_finite() for x in (before, observed, amount)) or before <= 0 or amount <= 0:
        return None
    # Exposure equality is an absolute check. A relative tolerance can hide
    # a one-contract partial fill on large positions and permit a duplicate close.
    tolerance = Decimal("1e-10")
    if abs(observed - before) > tolerance:
        return None
    retry = floor_amount_to_step(amount, 1)
    if retry <= 0 or retry >= float(amount) - 1e-10:
        return 0.0
    return retry



def fetch_order_by_client_id(fetch_order: Any, symbol: str, client_order_id: str) -> Any:
    """Query CCXT by the exact persisted client ID, with its two-argument fallback."""
    if not callable(fetch_order) or not str(client_order_id or "").strip():
        return None
    identity = str(client_order_id)
    try:
        return fetch_order(identity, symbol, {"clientOrderId": identity})
    except TypeError:
        try:
            return fetch_order(identity, symbol)
        except Exception:
            return None
    except Exception:
        return None
