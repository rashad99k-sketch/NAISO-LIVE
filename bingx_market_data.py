"""BingX USDT-M public BookTicker WebSocket feed.

The feed is intentionally limited to best bid/ask. Full depth is not maintained
here; consumers needing L2 must use a separately synchronized depth service.
"""
from __future__ import annotations

import gzip
import json
import random
import threading
import time
import uuid
import zlib
from typing import Any, Callable, Dict, Optional

try:
    import websocket
except Exception:  # Import remains safe in tests and non-market-data utilities.
    websocket = None


BINGX_SWAP_MARKET_WS = "wss://open-api-swap.bingx.com/swap-market"


def _canonical_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if ":" in value:
        value = value.split(":", 1)[0]
    value = value.replace("/", "-").replace("_", "-")
    if not value or any(ch in value for ch in "&=?#\r\n"):
        raise ValueError("invalid BingX market symbol")
    if not value.endswith("-USDT"):
        raise ValueError("only USDT-M symbols are supported")
    return value


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class BingXBookTickerFeed:
    """Threaded BookTicker subscriber with bounded reconnect and freshness checks."""

    def __init__(
        self,
        *,
        url: str = BINGX_SWAP_MARKET_WS,
        stale_after_sec: float = 1.5,
        reconnect_max_sec: float = 30.0,
        logger: Optional[Callable[[str], None]] = None,
        ws_app_factory: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ):
        self.url = url
        self.stale_after_sec = max(0.2, float(stale_after_sec))
        self.reconnect_max_sec = max(1.0, float(reconnect_max_sec))
        self.logger = logger or (lambda _message: None)
        self._factory = ws_app_factory
        self._clock = clock
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._connected = False
        self._desired: set[str] = set()
        self._books: Dict[str, Dict[str, Any]] = {}
        self._depths: Dict[tuple[str, int], Dict[str, Any]] = {}
        self._last_message_mono = 0.0
        self._last_error = ""
        self._reconnects = 0
        self._messages = 0

    @staticmethod
    def _channel(symbol: str) -> str:
        return f"{_canonical_symbol(symbol)}@bookTicker"

    @staticmethod
    def _depth_channel(symbol: str, level: int) -> str:
        canonical = _canonical_symbol(symbol)
        level = int(level)
        if level not in {5, 10, 20, 50, 100}:
            raise ValueError("unsupported BingX depth level")
        base = canonical.split("-", 1)[0]
        interval = "200ms" if base in {"BTC", "ETH"} else "500ms"
        return f"{canonical}@depth{level}@{interval}"

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            if self._factory is None and websocket is None:
                self._last_error = "websocket-client dependency is unavailable"
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="bingx-bookticker-ws",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def subscribe(self, symbol: str) -> bool:
        channel = self._channel(symbol)
        with self._lock:
            is_new = channel not in self._desired
            self._desired.add(channel)
            ws = self._ws if self._connected and is_new else None
        if ws is not None:
            self._send_subscribe(ws, channel)
        return self.start()

    def subscribe_depth(self, symbol: str, level: int = 10) -> bool:
        channel = self._depth_channel(symbol, level)
        with self._lock:
            is_new = channel not in self._desired
            self._desired.add(channel)
            ws = self._ws if self._connected and is_new else None
        if ws is not None:
            self._send_subscribe(ws, channel)
        return self.start()

    def get_depth(
        self, symbol: str, level: int = 10, *, max_age_sec: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        channel = self._depth_channel(symbol, level)
        canonical = channel.split("@", 1)[0]
        self.subscribe_depth(canonical, level)
        limit = self.stale_after_sec if max_age_sec is None else max(0.0, float(max_age_sec))
        with self._lock:
            item = self._depths.get((canonical, int(level)))
            if not item:
                return None
            age = max(0.0, self._clock() - float(item.get("received_mono", 0.0)))
            if age > limit:
                return None
            result = dict(item)
            result["bids"] = [list(row) for row in item["bids"]]
            result["asks"] = [list(row) for row in item["asks"]]
            result["age_sec"] = age
            return result

    def get(self, symbol: str, *, max_age_sec: Optional[float] = None) -> Optional[Dict[str, Any]]:
        channel = self._channel(symbol)
        canonical = channel.split("@", 1)[0]
        self.subscribe(canonical)
        limit = self.stale_after_sec if max_age_sec is None else max(0.0, float(max_age_sec))
        with self._lock:
            item = self._books.get(canonical)
            if not item:
                return None
            age = max(0.0, self._clock() - float(item.get("received_mono", 0.0)))
            if age > limit:
                return None
            result = dict(item)
            result["age_sec"] = age
            return result

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            ages = {
                symbol: round(max(0.0, now - float(item.get("received_mono", now))), 3)
                for symbol, item in self._books.items()
            }
            depth_ages = {
                f"{symbol}:{level}": round(max(0.0, now - float(item.get("received_mono", now))), 3)
                for (symbol, level), item in self._depths.items()
            }
            return {
                "connected": self._connected,
                "subscriptions": len(self._desired),
                "symbols": sorted(self._books),
                "book_ages_sec": ages,
                "depth_subscriptions": len(depth_ages),
                "depth_ages_sec": depth_ages,
                "last_message_age_sec": (
                    round(max(0.0, now - self._last_message_mono), 3)
                    if self._last_message_mono else None
                ),
                "messages": self._messages,
                "reconnects": self._reconnects,
                "last_error": self._last_error,
            }

    def _send_subscribe(self, ws: Any, channel: str) -> None:
        payload = {
            "id": uuid.uuid4().hex,
            "reqType": "sub",
            "dataType": channel,
        }
        try:
            ws.send(json.dumps(payload, separators=(",", ":")))
        except Exception as exc:
            with self._lock:
                self._last_error = f"subscribe failed: {type(exc).__name__}: {exc}"[:240]

    def _on_open(self, ws: Any) -> None:
        with self._lock:
            self._connected = True
            self._last_error = ""
            channels = sorted(self._desired)
        for channel in channels:
            self._send_subscribe(ws, channel)
        self.logger(f"[BINGX_WS] connected; bookTicker subscriptions={len(channels)}")

    def _on_message(self, ws: Any, message: Any) -> None:
        try:
            if isinstance(message, bytes):
                try:
                    raw = gzip.decompress(message).decode("utf-8")
                except (OSError, EOFError, zlib.error):
                    raw = message.decode("utf-8")
            else:
                raw = str(message)
            if raw.strip() in {"Ping", "ping", "PING"}:
                ws.send("Pong")
                return
            payload = json.loads(raw)
            if not isinstance(payload, dict) or int(payload.get("code", 0) or 0) != 0:
                return
            channel = str(payload.get("dataType") or "")
            data = payload.get("data")
            if not isinstance(data, dict):
                return
            received = self._clock()

            if channel.endswith("@bookTicker"):
                symbol = _canonical_symbol(data.get("s") or channel.split("@", 1)[0])
                bid = _number(data.get("b"))
                ask = _number(data.get("a"))
                bid_qty = _number(data.get("B")) or 0.0
                ask_qty = _number(data.get("A")) or 0.0
                if bid is None or ask is None or ask < bid:
                    return
                try:
                    exchange_ts_ms = int(data.get("T") or payload.get("ts") or 0)
                except (TypeError, ValueError, OverflowError):
                    exchange_ts_ms = 0
                with self._lock:
                    self._books[symbol] = {
                        "symbol": symbol, "bid": bid, "bid_qty": bid_qty,
                        "ask": ask, "ask_qty": ask_qty,
                        "exchange_ts_ms": exchange_ts_ms, "received_mono": received,
                    }
                    self._last_message_mono = received
                    self._messages += 1
                return

            if "@depth" in channel:
                symbol = _canonical_symbol(data.get("s") or channel.split("@", 1)[0])
                try:
                    level = int(channel.split("@depth", 1)[1].split("@", 1)[0])
                except (IndexError, TypeError, ValueError):
                    return
                if level not in {5, 10, 20, 50, 100}:
                    return

                def parse_levels(rows: Any, *, reverse: bool) -> list[list[float]]:
                    parsed: list[list[float]] = []
                    if not isinstance(rows, (list, tuple)):
                        return parsed
                    for row in rows:
                        if not isinstance(row, (list, tuple)) or len(row) < 2:
                            continue
                        price = _number(row[0])
                        try:
                            quantity = float(row[1])
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if price is None or quantity < 0 or quantity != quantity:
                            continue
                        parsed.append([price, quantity])
                    parsed.sort(key=lambda item: item[0], reverse=reverse)
                    return parsed[:level]

                bids = parse_levels(data.get("bids"), reverse=True)
                asks = parse_levels(data.get("asks"), reverse=False)
                if not bids or not asks or asks[0][0] < bids[0][0]:
                    return
                try:
                    server_ts_ms = int(payload.get("ts") or data.get("ts") or 0)
                except (TypeError, ValueError, OverflowError):
                    server_ts_ms = 0
                with self._lock:
                    self._depths[(symbol, level)] = {
                        "symbol": symbol, "level": level,
                        "bids": bids, "asks": asks,
                        "server_ts_ms": server_ts_ms,
                        "received_mono": received,
                    }
                    self._last_message_mono = received
                    self._messages += 1
        except Exception as exc:
            with self._lock:
                self._last_error = f"message parse failed: {type(exc).__name__}: {exc}"[:240]

    def _on_error(self, _ws: Any, error: Any) -> None:
        with self._lock:
            self._last_error = f"{type(error).__name__}: {error}"[:240]

    def _on_close(self, _ws: Any, _status: Any, _message: Any) -> None:
        with self._lock:
            self._connected = False

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            factory = self._factory or websocket.WebSocketApp
            try:
                app = factory(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                with self._lock:
                    self._ws = app
                app.run_forever(ping_interval=0)
            except Exception as exc:
                with self._lock:
                    self._last_error = f"connection failed: {type(exc).__name__}: {exc}"[:240]
            finally:
                with self._lock:
                    self._connected = False
                    self._ws = None
                    self._reconnects += 1
            if self._stop.is_set():
                break
            delay = min(backoff, self.reconnect_max_sec)
            delay += self._random_uniform(0.0, min(0.5, delay * 0.2))
            self._stop.wait(delay)
            backoff = min(backoff * 2.0, self.reconnect_max_sec)
