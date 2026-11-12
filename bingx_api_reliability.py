"""Bounded, read-only BingX API retry and back-pressure policy.

Only explicitly identified read methods may be retried. Order and account writes
are always single-shot; callers must reconcile ambiguous writes before acting again.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional


class ApiCooldownError(RuntimeError):
    """Raised while the shared read circuit is cooling down."""


class BingXApiReliability:
    READ_PREFIXES = (
        "fetch_", "load_markets", "swapv1privateget", "swapv2privateget",
        "swapv3privateget", "swapv1publicget", "swapv2publicget",
        "swapv3publicget", "publicget",
    )

    def __init__(
        self,
        *,
        max_attempts: int = 2,
        max_concurrent: int = 4,
        transient_delay: float = 0.25,
        rate_limit_cooldown: float = 1.0,
        ban_cooldown: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.max_attempts = max(1, min(int(max_attempts), 2))
        self.transient_delay = max(0.0, min(float(transient_delay), 1.0))
        self.rate_limit_cooldown = max(0.1, min(float(rate_limit_cooldown), 5.0))
        self.ban_cooldown = max(1.0, min(float(ban_cooldown), 300.0))
        self._sleep = sleep
        self._clock = clock
        self._logger = logger or (lambda _message: None)
        self._semaphore = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._lock = threading.RLock()
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self._stats = {
            "read_calls": 0, "read_retries": 0, "rate_limited": 0,
            "banned": 0, "transient_failures": 0, "fatal_failures": 0,
        }

    @classmethod
    def is_read_method(cls, method_name: str) -> bool:
        name = str(method_name or "").lower()
        return any(name.startswith(prefix) for prefix in cls.READ_PREFIXES)

    @staticmethod
    def _http_status(exc: BaseException) -> Optional[int]:
        for value in (
            getattr(exc, "status_code", None),
            getattr(exc, "http_status", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        text = str(exc).lower()
        for code in (418, 429, 500, 502, 503, 504):
            if str(code) in text and ("http" in text or "status" in text or "code" in text):
                return code
        return None

    @staticmethod
    def _retry_after(exc: BaseException) -> Optional[float]:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            headers = getattr(exc, "headers", None)
        if isinstance(headers, dict):
            raw = headers.get("Retry-After") or headers.get("retry-after")
            try:
                if raw is not None:
                    return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return None

    @classmethod
    def classify(cls, exc: BaseException) -> str:
        status = cls._http_status(exc)
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if status == 418 or "418" in message or "ipban" in name or "banned" in message:
            return "BAN"
        if status == 429 or "100410" in message or "rate limit" in message or "ratelimit" in name or "ddosprotection" in name:
            return "RATE_LIMIT"
        if status is not None and 500 <= status <= 599:
            return "TRANSIENT"
        if any(token in name for token in (
            "timeout", "networkerror", "exchangeunavailable", "serviceunavailable",
            "connectionerror", "temporarilyunavailable",
        )):
            return "TRANSIENT"
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return "TRANSIENT"
        return "FATAL"

    def _cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def _set_cooldown(self, seconds: float, reason: str) -> None:
        until = self._clock() + max(0.0, seconds)
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)
            self._cooldown_reason = str(reason)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            result = dict(self._stats)
            result["cooldown_remaining"] = max(0.0, self._cooldown_until - self._clock())
            result["cooldown_reason"] = self._cooldown_reason
            return result

    def call_read(
        self,
        func: Callable[..., Any],
        *args: Any,
        before_call: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> Any:
        method_name = getattr(func, "__name__", "")
        if not self.is_read_method(method_name):
            # Fail closed for unknown/write callables: no automatic replay.
            if before_call:
                before_call()
            return func(*args, **kwargs)

        if not self._semaphore.acquire(timeout=10.0):
            raise ApiCooldownError("BingX read concurrency limit reached")
        try:
            remaining = self._cooldown_remaining()
            if remaining > 0:
                raise ApiCooldownError(
                    f"BingX read circuit cooling down for {remaining:.2f}s ({self._cooldown_reason})"
                )
            for attempt in range(self.max_attempts):
                if before_call:
                    before_call()
                with self._lock:
                    self._stats["read_calls"] += 1
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    kind = self.classify(exc)
                    if kind == "BAN":
                        with self._lock:
                            self._stats["banned"] += 1
                        self._set_cooldown(self.ban_cooldown, "HTTP_418_BAN")
                        self._logger(f"[BINGX_API] HTTP 418 ban detected; read circuit paused for {self.ban_cooldown:.0f}s")
                        raise
                    if kind == "RATE_LIMIT":
                        with self._lock:
                            self._stats["rate_limited"] += 1
                        retry_after = self._retry_after(exc)
                        wait = retry_after if retry_after is not None else self.rate_limit_cooldown
                        # Only a small, explicit Retry-After may be retried once.
                        if attempt + 1 < self.max_attempts and retry_after is not None and 0 < retry_after <= 1.0:
                            with self._lock:
                                self._stats["read_retries"] += 1
                            self._logger(f"[BINGX_API] Rate limited; one bounded read retry after {wait:.2f}s")
                            self._sleep(wait)
                            continue
                        self._set_cooldown(min(max(wait, self.rate_limit_cooldown), 60.0), "HTTP_429_RATE_LIMIT")
                        raise
                    if kind == "TRANSIENT":
                        with self._lock:
                            self._stats["transient_failures"] += 1
                        if attempt + 1 < self.max_attempts:
                            with self._lock:
                                self._stats["read_retries"] += 1
                            self._logger(
                                f"[BINGX_API] Transient read failure in {method_name}; "
                                f"retry {attempt + 2}/{self.max_attempts}"
                            )
                            self._sleep(self.transient_delay)
                            continue
                        raise
                    with self._lock:
                        self._stats["fatal_failures"] += 1
                    raise
            raise ApiCooldownError("BingX read attempts exhausted")
        finally:
            self._semaphore.release()
