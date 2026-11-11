"""Market-data refresh + indicator snapshot integrity for open-trade management.

Root cause this module addresses: the live management path requested
`get_ohlcv_safe(symbol, 80)` while the OHLCV fetcher rejected any frame with fewer
than 100 candles, so the frame was always None and ADX/DI/continuation/state were
never refreshed after entry (they stayed at their entry values).

This module keeps two clearly separated snapshots:

  * CONFIRMED snapshot: computed from CLOSED candles only. It does not change
    until a new closed candle appears. Used for decisions that require candle
    confirmation.
  * LIVE / PROVISIONAL snapshot: optionally computed from the still-forming
    candle. It is labelled provisional and must never replace the confirmed
    indicators or trigger close-confirmed entry decisions.

Every snapshot carries provenance (symbol, timeframe, source, fetch time, last
closed candle, current candle, calculation time, candle count, version, type,
freshness) so a stale value can never be presented as fresh.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# Minimum closed candles required by the longest warm-up used in the engine
# (EMA50 and ADX with a 14-period RMA double smoothing). 60 leaves a safe margin.
REQUIRED_CLOSED_CANDLES = 60
# Default OHLCV limit the management path must request. Must be >= the fetch
# minimum so the frame is never silently dropped.
MANAGEMENT_OHLCV_LIMIT = 120
# The hard minimum the OHLCV fetcher enforces (fetch_ohlcv returns None below it).
FETCH_MIN_CANDLES = 100

FRESH = "FRESH"
UNCHANGED_BUT_VALID = "UNCHANGED_BUT_VALID"
STALE = "STALE"
INVALID = "INVALID"
INSUFFICIENT = "INSUFFICIENT"

CONFIRMED = "CONFIRMED"
LIVE_PROVISIONAL = "LIVE_PROVISIONAL"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return number
    except (TypeError, ValueError, OverflowError):
        return default


def timeframe_seconds(timeframe: str) -> int:
    tf = str(timeframe or "15m").strip().lower()
    try:
        if tf.endswith("m"):
            return int(tf[:-1]) * 60
        if tf.endswith("h"):
            return int(tf[:-1]) * 3600
        if tf.endswith("d"):
            return int(tf[:-1]) * 86400
    except (TypeError, ValueError):
        pass
    return 15 * 60


def split_closed_and_current(frame, timeframe: str) -> Tuple[Any, Any, Optional[float], Optional[float]]:
    """Return (closed_frame, current_candle_row, last_closed_ts, current_ts).

    A candle is considered still forming when `now < candle_open_ts + duration`.
    When timestamps are unavailable the whole frame is treated as closed.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame, None, None, None
    duration = timeframe_seconds(timeframe)
    try:
        if "timestamp" in getattr(frame, "columns", []):
            raw = float(frame["timestamp"].iloc[-1])
        else:
            raw = float(frame.index[-1])
    except Exception:
        return frame, None, None, None
    last_open_ts = raw / 1000.0 if raw > 1e12 else raw
    if time.time() < last_open_ts + duration:
        closed = frame.iloc[:-1].copy()
        current = frame.iloc[-1]
        last_closed_ts = None
        if len(closed) > 0:
            try:
                v = float(closed["timestamp"].iloc[-1]) if "timestamp" in closed.columns else float(closed.index[-1])
                last_closed_ts = v / 1000.0 if v > 1e12 else v
            except Exception:
                last_closed_ts = None
        return closed, current, last_closed_ts, last_open_ts
    # The last candle is already closed.
    try:
        v = float(frame["timestamp"].iloc[-1]) if "timestamp" in frame.columns else float(frame.index[-1])
        last_closed_ts = v / 1000.0 if v > 1e12 else v
    except Exception:
        last_closed_ts = None
    return frame, None, last_closed_ts, None


@dataclass
class IndicatorSnapshot:
    symbol: str
    timeframe: str
    source: str
    fetch_timestamp: float
    last_closed_candle_timestamp: Optional[float]
    current_candle_timestamp: Optional[float]
    calculation_timestamp: float
    candle_count: int
    snapshot_version: int
    snapshot_type: str
    data_freshness: str
    indicators: Dict[str, Any] = field(default_factory=dict)

    def as_log(self) -> str:
        ind = self.indicators
        return (
            f"[INDICATOR_REFRESH]\n"
            f"  Symbol: {self.symbol}\n"
            f"  ADX: {_num(ind.get('adx')):.2f}\n"
            f"  DI_PLUS: {_num(ind.get('di_plus')):.2f}\n"
            f"  DI_MINUS: {_num(ind.get('di_minus')):.2f}\n"
            f"  RSI: {_num(ind.get('rsi')):.2f}\n"
            f"  ATR: {_num(ind.get('atr')):.8f}\n"
            f"  EMA: {ind.get('ema_state', 'n/a')}\n"
            f"  VWAP: {_num(ind.get('vwap')):.8f}\n"
            f"  Volume: {_num(ind.get('volume')):.4f}\n"
            f"  Continuation: {_num(ind.get('continuation_probability'), 0.5):.2f}\n"
            f"  Market Regime: {ind.get('market_regime', 'n/a')}\n"
            f"  Snapshot Type: {self.snapshot_type}\n"
            f"  Snapshot Version: {self.snapshot_version}\n"
            f"  Last Closed Candle: {self.last_closed_candle_timestamp}\n"
            f"  Calculation Timestamp: {self.calculation_timestamp:.3f}\n"
            f"  Freshness: {self.data_freshness}\n"
            f"  Update Status: {'UPDATED' if self.data_freshness in (FRESH, UNCHANGED_BUT_VALID) else self.data_freshness}"
        )


class MarketDataRefresh:
    """Fetch + validate OHLCV for one open symbol and build indicator snapshots.

    It performs NO indicator math itself (that stays in ggpbot's engines); it only
    fetches, validates freshness/counts, separates closed vs forming candles, and
    stamps provenance. Indicator values are supplied by the caller's calculator.
    """

    def __init__(self, *, fetch_fn, timeframe: str, source: str = "BINGX_OHLCV",
                 required_closed: int = REQUIRED_CLOSED_CANDLES,
                 limit: int = MANAGEMENT_OHLCV_LIMIT):
        self._fetch_fn = fetch_fn
        self.timeframe = timeframe
        self.source = source
        self.required_closed = required_closed
        self.limit = limit
        self._confirmed: Optional[IndicatorSnapshot] = None
        self._version = 0

    @property
    def confirmed(self) -> Optional[IndicatorSnapshot]:
        return self._confirmed

    def refresh(self, symbol: str, *, calc_indicators, calc_live_indicators=None,
                now: Optional[float] = None) -> Dict[str, Any]:
        """Fetch, validate, and (re)build snapshots. Returns a status dict.

        `calc_indicators(closed_frame) -> dict` computes confirmed indicators.
        `calc_live_indicators(closed_frame, current_row) -> dict` optionally
        computes provisional indicators from the forming candle.
        """
        now = now if now is not None else time.time()
        status: Dict[str, Any] = {
            "symbol": symbol, "timeframe": self.timeframe, "fetch_time": now,
            "fetch_status": "UNKNOWN", "last_closed_candle": None, "current_candle": None,
            "received_candle_count": 0, "required_candle_count": self.required_closed,
            "ohlcv_freshness": INVALID, "cache_status": "MISS",
        }
        try:
            frame = self._fetch_fn(symbol, self.limit)
        except Exception as exc:
            status["fetch_status"] = f"ERROR:{type(exc).__name__}"
            status["ohlcv_freshness"] = INVALID
            return status
        if frame is None or getattr(frame, "empty", True):
            status["fetch_status"] = "NONE"
            status["ohlcv_freshness"] = INVALID
            return status
        status["fetch_status"] = "OK"
        status["received_candle_count"] = int(len(frame))

        closed, current_row, last_closed_ts, current_ts = split_closed_and_current(frame, self.timeframe)
        status["last_closed_candle"] = last_closed_ts
        status["current_candle"] = current_ts
        closed_count = 0 if closed is None else int(len(closed))

        if closed_count < self.required_closed:
            status["ohlcv_freshness"] = INSUFFICIENT
            # Do not fabricate indicators; keep the previous confirmed snapshot.
            return status

        # Freshness: compare against the previously seen closed candle.
        prev_closed_ts = self._confirmed.last_closed_candle_timestamp if self._confirmed else None
        if prev_closed_ts is not None and last_closed_ts is not None and last_closed_ts == prev_closed_ts:
            freshness = UNCHANGED_BUT_VALID
        elif prev_closed_ts is not None and last_closed_ts is not None and last_closed_ts < prev_closed_ts:
            # Data went backwards: stale/duplicate feed.
            freshness = STALE
        else:
            freshness = FRESH
        status["ohlcv_freshness"] = freshness

        # Recompute the CONFIRMED snapshot only when the closed candle changed.
        if freshness == FRESH:
            indicators = dict(calc_indicators(closed) or {})
            self._version += 1
            self._confirmed = IndicatorSnapshot(
                symbol=symbol, timeframe=self.timeframe, source=self.source,
                fetch_timestamp=now, last_closed_candle_timestamp=last_closed_ts,
                current_candle_timestamp=current_ts, calculation_timestamp=now,
                candle_count=closed_count, snapshot_version=self._version,
                snapshot_type=CONFIRMED, data_freshness=FRESH, indicators=indicators)
            status["snapshot_version"] = self._version
            status["confirmed_indicators"] = indicators
        elif freshness == UNCHANGED_BUT_VALID and self._confirmed is not None:
            # Keep the confirmed snapshot as-is; it is still valid.
            status["snapshot_version"] = self._confirmed.snapshot_version
            status["confirmed_indicators"] = dict(self._confirmed.indicators)
        # STALE / INSUFFICIENT: do not publish new confirmed indicators.

        # Optional LIVE/PROVISIONAL snapshot from the forming candle.
        if calc_live_indicators is not None and current_row is not None and closed_count >= 1:
            try:
                live_indicators = dict(calc_live_indicators(closed, current_row) or {})
                self._version += 1
                self._live = IndicatorSnapshot(
                    symbol=symbol, timeframe=self.timeframe, source=self.source,
                    fetch_timestamp=now, last_closed_candle_timestamp=last_closed_ts,
                    current_candle_timestamp=current_ts, calculation_timestamp=now,
                    candle_count=closed_count, snapshot_version=self._version,
                    snapshot_type=LIVE_PROVISIONAL, data_freshness=FRESH,
                    indicators=live_indicators)
                status["live_indicators"] = live_indicators
                status["live_snapshot_version"] = self._version
            except Exception as exc:
                status["live_error"] = f"{type(exc).__name__}: {exc}"
        return status
