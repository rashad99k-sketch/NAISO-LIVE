"""Bounded asynchronous Telegram notifier for GGP operational alerts."""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import requests


class TelegramNotifier:
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, token: str = "", chat_id: str = "", timeout: float = 8.0,
                 max_pending: int = 100, post: Optional[Callable] = None,
                 on_error: Optional[Callable[[str], None]] = None,
                 max_retries: int = 2):
        self._token = str(token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._timeout = max(2.0, min(20.0, float(timeout or 8.0)))
        self._max_retries = max(0, min(3, int(max_retries)))
        self._queue = queue.Queue(maxsize=max(10, int(max_pending)))
        self._post = post or requests.post
        self._on_error = on_error
        self._worker_lock = threading.Lock()
        self._worker_started = False
        self._worker = None
        self._closed = False
        self._last_error = None
        self._sent = 0
        self._failed = 0
        self._dropped = 0

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "worker_running": bool(self._worker and self._worker.is_alive()),
            "queue_depth": self._queue.qsize(),
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    def enqueue(self, text: str) -> bool:
        if not self.configured or self._closed:
            return False
        message = str(text or "").strip()
        if not message:
            return False
        if len(message) > 3900:
            message = message[:3880] + "\\n… [message truncated]"
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            self._dropped += 1
            self._report_error("Telegram notification queue is full; message dropped")
            return False
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started or self._closed:
                return
            self._worker = threading.Thread(target=self._run, daemon=True, name="ggp_telegram_worker")
            self._worker.start()
            self._worker_started = True

    def _report_error(self, message: str) -> None:
        self._last_error = str(message)[:180]
        if self._on_error:
            try:
                self._on_error(self._last_error)
            except Exception:
                pass

    def _deliver(self, message: str) -> bool:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._post(
                    url,
                    json={"chat_id": self._chat_id, "text": message,
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=self._timeout,
                )
                status = int(getattr(response, "status_code", 200) or 200)
                if status in self.RETRYABLE_STATUS and attempt < self._max_retries:
                    retry_after = 0.0
                    try:
                        retry_after = float(getattr(response, "headers", {}).get("Retry-After", 0) or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(max(0.1, min(2.0, retry_after or (0.25 * (2 ** attempt)))))
                    continue
                response.raise_for_status()
                try:
                    result = response.json()
                except Exception:
                    result = {}
                if isinstance(result, dict) and result.get("ok") is True:
                    self._sent += 1
                    self._last_error = None
                    return True
                error_code = int(result.get("error_code", 0) or 0) if isinstance(result, dict) else 0
                description = result.get("description", "Telegram returned an invalid response") if isinstance(result, dict) else "Telegram returned an invalid response"
                if error_code in self.RETRYABLE_STATUS and attempt < self._max_retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                self._report_error(f"Telegram API rejected notification: {str(description)[:140]}")
                self._failed += 1
                return False
            except requests.RequestException as exc:
                if attempt < self._max_retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                self._report_error(f"Telegram send failed: {type(exc).__name__}")
                self._failed += 1
                return False
            except Exception as exc:
                self._report_error(f"Telegram send failed: {type(exc).__name__}")
                self._failed += 1
                return False
        self._failed += 1
        self._report_error("Telegram send failed after retry limit")
        return False

    def _run(self) -> None:
        while not self._closed or not self._queue.empty():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(message)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 5.0) -> None:
        self._closed = True
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=max(0.1, min(10.0, float(timeout))))
