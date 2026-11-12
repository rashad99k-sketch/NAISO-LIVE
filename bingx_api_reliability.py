"""Bounded, read-only BingX API retry and back-pressure policy.

Only explicitly identified read methods may be retried. Order and account writes
are always single-shot; callers must reconcile ambiguous writes before acting again.
"""
from __future__ import annotations

import random
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
        rate_limit_cooldown: float = 10.0,
        ban_cooldown: float = 900.0,
        system_busy_cooldown: float = 20.0,
        risk_control_cooldown: float = 60.0,
        endpoint_min_intervals: Optional[Dict[str, float]] = None,
        retry_jitter_ratio: float = 0.20,
        random_uniform: Callable[[float, float], float] = random.uniform,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.max_attempts = max(1, min(int(max_attempts), 2))
        self.transient_delay = max(0.0, min(float(transient_delay), 1.0))
        self.rate_limit_cooldown = max(1.0, min(float(rate_limit_cooldown), 60.0))
        self.ban_cooldown = max(60.0, min(float(ban_cooldown), 3600.0))
        self.system_busy_cooldown = max(1.0, min(float(system_busy_cooldown), 300.0))
        self.risk_control_cooldown = max(1.0, min(float(risk_control_cooldown), 600.0))
        self.endpoint_min_intervals = dict(endpoint_min_intervals or {
            # BingX public market-data REST endpoints documented at 1 request/second/IP.
            "fetch_order_book": 1.05,
            "fetch_ticker": 1.05,
            "fetch_ohlcv": 1.05,
            "fetch_trades": 1.05,
            "load_markets": 1.05,
        })
        self._endpoint_next_at: Dict[str, float] = {}
        self._sleep = sleep
        self._clock = clock
        self._logger = logger or (lambda _message: None)
        self._semaphore = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._lock = threading.RLock()
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self._recovery_probe_required = False
        self._stats = {
            "read_calls": 0, "read_retries": 0, "rate_limited": 0,
            "banned": 0, "transient_failures": 0, "fatal_failures": 0,
            "system_busy": 0, "risk_control": 0, "endpoint_waits": 0,
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
        if "109500" in message or "current system is busy" in message:
            return "SYSTEM_BUSY"
        if "109403" in message or "109429" in message:
            return "RISK_CONTROL"
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

    def assert_new_entry_allowed(self) -> None:
        """Block only new entries during an API circuit cooldown; exits/protection bypass this gate."""
        remaining = self._cooldown_remaining()
        with self._lock:
            reason = self._cooldown_reason
            recovery_probe_required = self._recovery_probe_required
        if remaining > 0 or recovery_probe_required:
            raise ApiCooldownError(
                f"new entry blocked by BingX API circuit (cooldown={remaining:.2f}s; "
                f"recovery_probe_required={recovery_probe_required}; reason={reason})"
            )

    def observe_error(self, exc: BaseException, method_name: str = "write") -> str:
        """Record rate-control errors from single-shot writes without retrying them."""
        kind = self.classify(exc)
        retry_after = self._retry_after(exc)
        if kind == "BAN":
            with self._lock:
                self._stats["banned"] += 1
            self._set_cooldown(max(self.ban_cooldown, retry_after or 0.0), f"HTTP_418_BAN:{method_name}")
        elif kind == "RATE_LIMIT":
            with self._lock:
                self._stats["rate_limited"] += 1
            self._set_cooldown(max(self.rate_limit_cooldown, retry_after or 0.0), f"BINGX_RATE_LIMIT:{method_name}")
        elif kind == "SYSTEM_BUSY":
            with self._lock:
                self._stats["system_busy"] += 1
            self._set_cooldown(max(self.system_busy_cooldown, retry_after or 0.0), f"BINGX_109500:{method_name}")
        elif kind == "RISK_CONTROL":
            with self._lock:
                self._stats["risk_control"] += 1
            self._set_cooldown(max(self.risk_control_cooldown, retry_after or 0.0), f"BINGX_RISK_CONTROL:{method_name}")
        return kind

    def _set_cooldown(self, seconds: float, reason: str) -> None:
        until = self._clock() + max(0.0, seconds)
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)
            self._cooldown_reason = str(reason)
            self._recovery_probe_required = True

    def _mark_recovery_probe_success(self) -> None:
        with self._lock:
            if self._recovery_probe_required and self._cooldown_until <= self._clock():
                self._recovery_probe_required = False
                self._cooldown_reason = ""

    def _pace_endpoint(self, method_name: str) -> None:
        interval = max(0.0, float(self.endpoint_min_intervals.get(method_name, 0.0)))
        if interval <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = max(now, self._endpoint_next_at.get(method_name, now))
            self._endpoint_next_at[method_name] = slot + interval
            wait = max(0.0, slot - now)
            if wait > 0:
                self._stats["endpoint_waits"] += 1
        if wait > 0:
            self._sleep(wait)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            result = dict(self._stats)
            result["cooldown_remaining"] = max(0.0, self._cooldown_until - self._clock())
            result["cooldown_reason"] = self._cooldown_reason
            result["recovery_probe_required"] = self._recovery_probe_required
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
                self._pace_endpoint(method_name)
                with self._lock:
                    self._stats["read_calls"] += 1
                try:
                    result = func(*args, **kwargs)
                    if method_name != "load_markets":
                        self._mark_recovery_probe_success()
                    return result
                except Exception as exc:
                    kind = self.classify(exc)
                    if kind == "BAN":
                        with self._lock:
                            self._stats["banned"] += 1
                        retry_after = self._retry_after(exc)
                        cooldown = max(self.ban_cooldown, retry_after or 0.0)
                        self._set_cooldown(cooldown, "HTTP_418_BAN")
                        self._logger(f"[BINGX_API] HTTP 418 ban detected; API circuit paused for {cooldown:.0f}s")
                        raise
                    if kind == "SYSTEM_BUSY":
                        with self._lock:
                            self._stats["system_busy"] += 1
                        retry_after = self._retry_after(exc)
                        cooldown = max(self.system_busy_cooldown, retry_after or 0.0)
                        self._set_cooldown(cooldown, "BINGX_109500_SYSTEM_BUSY")
                        self._logger(
                            f"[BINGX_API] 109500 system busy; reads paused for {cooldown:.0f}s"
                        )
                        raise
                    if kind == "RISK_CONTROL":
                        with self._lock:
                            self._stats["risk_control"] += 1
                        retry_after = self._retry_after(exc)
                        cooldown = max(self.risk_control_cooldown, retry_after or 0.0)
                        self._set_cooldown(cooldown, "BINGX_RISK_CONTROL")
                        self._logger(
                            f"[BINGX_API] risk-control response; reads paused for {min(cooldown, 3600.0):.0f}s"
                        )
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
                        self._set_cooldown(max(wait, self.rate_limit_cooldown), "HTTP_429_RATE_LIMIT")
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
                            base_delay = self.transient_delay * (2 ** attempt)
                            jitter = (
                                self._random_uniform(
                                    1.0 - self.retry_jitter_ratio,
                                    1.0 + self.retry_jitter_ratio,
                                )
                                if self.retry_jitter_ratio > 0 else 1.0
                            )
                            self._sleep(min(base_delay * jitter, 2.0))
                            continue
                        raise
                    with self._lock:
                        self._stats["fatal_failures"] += 1
                    raise
            raise ApiCooldownError("BingX read attempts exhausted")
        finally:
            self._semaphore.release()
