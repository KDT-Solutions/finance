"""Brute-force protection: account lock-out independent of the client address, MCP bearer throttle."""

import time

import pytest
from argon2 import PasswordHasher

from app import auth
from app.config import get_settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_user", "admin")
    monkeypatch.setattr(s, "admin_password_hash", PasswordHasher().hash("correct horse"))
    monkeypatch.setattr(s, "admin_totp_secret", "")
    monkeypatch.setattr(auth, "_alert_lockout", lambda seconds, ip: None)
    auth.reset_lockout()
    yield
    auth.reset_lockout()


def test_correct_password_passes_and_clears_failures():
    assert auth.check_password("admin", "wrong", "10.0.0.1") is False
    assert auth.check_password("admin", "correct horse", "10.0.0.1") is True
    assert auth.locked_for() == 0


def test_lockout_is_independent_of_the_client_address():
    # every attempt from a different (spoofed) address: the per-IP limit never triggers ...
    for i in range(auth.LOCK_AFTER):
        assert auth.check_password("admin", "wrong", f"10.0.{i}.1") is False
    # ... but the account is locked now, even for the right password from a fresh address
    assert auth.locked_for() > 0
    assert auth.check_password("admin", "correct horse", "10.9.9.9") is False


def test_lock_duration_doubles_per_further_failure(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    for _ in range(auth.LOCK_AFTER):
        auth.check_password("admin", "wrong", "10.0.0.1")
    assert auth.locked_for() == auth.LOCK_BASE
    now[0] += auth.LOCK_BASE + 1
    assert auth.locked_for() == 0
    auth.check_password("admin", "wrong", "10.0.0.2")
    assert auth.locked_for() == auth.LOCK_BASE * 2
    now[0] += auth.LOCK_BASE * 2 + 1
    for _ in range(20):
        auth.check_password("admin", "wrong", "10.0.0.3")
        now[0] += auth.LOCK_MAX + 1
    auth.check_password("admin", "wrong", "10.0.0.3")
    assert auth.locked_for() == auth.LOCK_MAX


def test_lock_expires_and_success_resets(monkeypatch):
    now = [2_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    for _ in range(auth.LOCK_AFTER):
        auth.check_password("admin", "wrong", "10.0.0.1")
    assert auth.check_password("admin", "correct horse", "10.0.0.1") is False
    now[0] += auth.LOCK_BASE + 1
    # the address used for the failures stays on its own per-IP limit for IP_WINDOW seconds
    assert auth.check_password("admin", "correct horse", "10.0.0.1") is False
    assert auth.check_password("admin", "correct horse", "10.0.0.2") is True
    assert auth.locked_for() == 0
    assert auth.check_password("admin", "wrong", "10.0.0.2") is False  # counts from zero again
    assert auth.locked_for() == 0


def test_totp_failures_count_towards_the_same_lock(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_totp_secret", "JBSWY3DPEHPK3PXP")
    for _ in range(auth.LOCK_AFTER):
        assert auth.check_totp("000000", "10.0.0.1") is False
    assert auth.locked_for() > 0
    assert auth.check_password("admin", "correct horse", "10.0.0.2") is False


def test_lockout_alert_once_per_lock_period(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "_alert_lockout", lambda seconds, ip: calls.append((seconds, ip)))
    for _ in range(auth.LOCK_AFTER + 3):
        auth.check_password("admin", "wrong", "10.0.0.1")
    assert calls == [(auth.LOCK_BASE, "10.0.0.1")]


def test_mcp_throttle_after_wrong_tokens(monkeypatch):
    now = [3_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    assert auth.mcp_blocked() is False
    for _ in range(auth.MCP_FAIL_LIMIT - 1):
        auth.mcp_failed()
    assert auth.mcp_blocked() is False
    auth.mcp_failed()
    assert auth.mcp_blocked() is True
    now[0] += auth.MCP_FAIL_WINDOW + 1
    assert auth.mcp_blocked() is False
