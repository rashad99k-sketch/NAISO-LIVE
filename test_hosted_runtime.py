"""Hosted deployment safety gates and health/readiness contract tests."""
import ggpbot


def _set_hosted_state(monkeypatch, *, ready):
    monkeypatch.setattr(ggpbot, "HOSTED_RUNTIME_REQUIRED", True)
    with ggpbot.RUNTIME_STATUS_LOCK:
        ggpbot.RUNTIME_STATUS["ready"] = ready
        ggpbot.RUNTIME_STATUS["last_error"] = None if ready else "RuntimeError: test-not-ready"


def test_health_is_liveness_even_when_exchange_is_not_ready(monkeypatch):
    _set_hosted_state(monkeypatch, ready=False)

    response = ggpbot.app.test_client().get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "alive"
    assert payload["ready"] is False


def test_readiness_returns_503_until_preflight_and_recovery_complete(monkeypatch):
    _set_hosted_state(monkeypatch, ready=False)

    response = ggpbot.app.test_client().get("/ready")

    assert response.status_code == 503
    assert response.get_json()["ready"] is False


def test_readiness_returns_200_after_runtime_is_ready(monkeypatch):
    _set_hosted_state(monkeypatch, ready=True)

    response = ggpbot.app.test_client().get("/ready")

    assert response.status_code == 200
    assert response.get_json()["ready"] is True


def test_hosted_order_submission_is_blocked_before_readiness(monkeypatch):
    _set_hosted_state(monkeypatch, ready=False)
    called = []
    monkeypatch.setattr(
        ggpbot, "_original_open_position_hosted",
        lambda *args, **kwargs: called.append((args, kwargs)) or {"id": "must-not-submit"},
    )

    result = ggpbot.open_position("BUY", 1.0, "BTC/USDT")

    assert result is None
    assert called == []


def test_invalid_host_port_fails_with_actionable_error(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")

    try:
        ggpbot.start_dashboard_servers()
    except RuntimeError as exc:
        assert "invalid PORT" in str(exc)
    else:
        raise AssertionError("invalid hosted PORT must fail startup")
