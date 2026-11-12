"""Hosted environment detection and safe runtime mode resolution tests."""
from ggp_runtime_config import is_hosted_runtime, resolve_trading_mode


def test_port_alone_identifies_hosted_web_service():
    assert is_hosted_runtime({"PORT": "8080"}) is True


def test_provider_markers_identify_hosted_runtime_without_port():
    assert is_hosted_runtime({"RAILWAY_ENVIRONMENT": "production"}) is True
    assert is_hosted_runtime({"RENDER": "true"}) is True


def test_blank_port_and_missing_markers_are_local():
    assert is_hosted_runtime({"PORT": "  "}) is False
    assert is_hosted_runtime({}) is False


def test_platform_environment_keeps_live_switches_and_credentials_authoritative():
    env = {
        "PORT": "8080",
        "PAPER_MODE": "false",
        "ENABLE_LIVE_TRADING": "true",
        "BINGX_API_KEY": "platform-key",
        "BINGX_API_SECRET": "platform-secret",
    }
    paper, live, _reason = resolve_trading_mode(env)
    assert paper is False
    assert live is True
