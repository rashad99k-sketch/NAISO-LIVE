import threading
from ggp_telegram import TelegramNotifier


class _Response:
    def raise_for_status(self):
        return None
    def json(self):
        return {"ok": True}


def test_telegram_is_disabled_without_credentials():
    notifier = TelegramNotifier("", "")
    assert notifier.configured is False
    assert notifier.enqueue("hello") is False


def test_telegram_enqueue_uses_bounded_queue_and_no_secrets_in_status():
    sent = threading.Event()
    seen = []
    def fake_post(url, **kwargs):
        assert "bot-token-private" in url
        seen.append(kwargs["json"]["text"])
        sent.set()
        return _Response()
    notifier = TelegramNotifier("bot-token-private", "chat-id-private", max_pending=10, post=fake_post)
    assert notifier.enqueue("<b>TP1 verified</b>") is True
    assert sent.wait(2)
    status = notifier.status()
    assert status["configured"] is True
    assert status["sent"] == 1
    assert seen == ["<b>TP1 verified</b>"]
    assert "bot-token-private" not in str(status)
    assert "chat-id-private" not in str(status)
    notifier.close()


class _RetryResponse(_Response):
    def __init__(self, status_code=200, ok=True):
        self.status_code = status_code
        self._ok = ok
        self.headers = {}
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("http error")
    def json(self):
        return {"ok": self._ok}


def test_telegram_retries_transient_server_errors_then_delivers():
    sent = threading.Event()
    attempts = []
    def fake_post(url, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return _RetryResponse(status_code=503, ok=False)
        sent.set()
        return _RetryResponse()
    notifier = TelegramNotifier("token", "chat", post=fake_post, max_retries=1)
    assert notifier.enqueue("Trade opened")
    assert sent.wait(2)
    notifier._queue.join()
    assert len(attempts) == 2
    assert notifier.status()["sent"] == 1
    assert notifier.status()["failed"] == 0
    notifier.close()


def test_telegram_reports_permanent_api_rejection_without_leaking_credentials():
    errors = []
    notifier = TelegramNotifier("secret-token", "private-chat", post=lambda *a, **k: _RetryResponse(ok=False),
                                on_error=errors.append, max_retries=0)
    assert notifier.enqueue("bad chat")
    notifier._queue.join()
    status = notifier.status()
    assert status["failed"] == 1
    assert errors and "secret-token" not in str(errors)
    assert "private-chat" not in str(status)
    notifier.close()


def test_telegram_close_drains_already_queued_notifications():
    sent = []
    def fake_post(url, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _RetryResponse()
    notifier = TelegramNotifier("token", "chat", post=fake_post)
    assert notifier.enqueue("first")
    assert notifier.enqueue("second")
    notifier.close(timeout=2)
    assert sent == ["first", "second"]
