"""Single-admin login (argon2 password + optional TOTP), signed session cookie, CSRF token.

Brute-force protection works on two levels, both independent of the client IP, because the IP
is only as trustworthy as the proxy chain in front of uvicorn (X-Forwarded-For):

* Account lock-out: after LOCK_AFTER failed password or TOTP attempts the single admin account is
  locked for a delay that doubles with every further failure (LOCK_BASE .. LOCK_MAX seconds).
  While locked, no credential is verified at all. The lock is cleared by a successful login or
  when the delay has passed. A lock-out triggers a Pushover alert (once per lock period).
* Per-IP limit on top of that (IP_MAX_ATTEMPTS per IP_WINDOW seconds), so a single misbehaving
  client is cut off earlier than the account lock would.

MCP bearer tokens are throttled with the same helper (see `mcp_failed`/`mcp_blocked`): valid
tokens always pass, but after MCP_FAIL_LIMIT wrong tokens the endpoint slows down and answers
429 to unauthenticated requests for MCP_FAIL_WINDOW seconds.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

log = logging.getLogger(__name__)

COOKIE = "finance_session"
PRE_COOKIE = "finance_pre"  # password verified, TOTP pending (second step)
SESSION_MAX_AGE = 12 * 3600
PRE_MAX_AGE = 300

# account lock-out (independent of the client address)
LOCK_AFTER = 5          # failed attempts before the first lock
LOCK_BASE = 60          # seconds for the first lock, doubles per further failure
LOCK_MAX = 900          # cap for the lock duration
FAIL_MEMORY = 3600      # a failure older than this no longer counts

# per-IP limit (secondary; the address may be spoofed if the proxy chain is misconfigured)
IP_MAX_ATTEMPTS = 5
IP_WINDOW = 900

# MCP bearer throttle
MCP_FAIL_LIMIT = 10
MCP_FAIL_WINDOW = 600

_ph = PasswordHasher()
_lock = threading.Lock()
_failed_ip: dict[str, list[float]] = {}
_account = {"failures": 0, "locked_until": 0.0, "last_failure": 0.0, "alerted_until": 0.0}
_mcp_failures: list[float] = []


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="session")


def create_session() -> str:
    return _serializer().dumps({"u": get_settings().admin_user, "csrf": secrets.token_urlsafe(16)})


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


# --- account lock-out -------------------------------------------------------------------

def _alert_lockout(seconds: int, client_ip: str) -> None:
    """Pushover once per lock period; never raises (the login must not depend on Pushover)."""
    try:
        from .services.notify import pushover

        pushover("finance: Login gesperrt", f"{_account['failures']} Fehlversuche, Konto für {seconds} s gesperrt (letzte Adresse {client_ip}).")
    except Exception as exc:  # pragma: no cover - notification is best effort
        log.warning("lockout alert failed: %s", exc)


def locked_for() -> int:
    """Seconds the admin account stays locked, 0 when open."""
    with _lock:
        return max(0, int(_account["locked_until"] - time.time()))


def _account_blocked() -> bool:
    now = time.time()
    with _lock:
        if now - _account["last_failure"] > FAIL_MEMORY:
            _account["failures"] = 0
        return _account["locked_until"] > now


def _account_record(ok: bool, client_ip: str) -> None:
    now = time.time()
    alert: int | None = None
    with _lock:
        if ok:
            _account.update(failures=0, locked_until=0.0, last_failure=0.0, alerted_until=0.0)
            return
        _account["failures"] += 1
        _account["last_failure"] = now
        if _account["failures"] >= LOCK_AFTER:
            delay = min(LOCK_MAX, LOCK_BASE * 2 ** (_account["failures"] - LOCK_AFTER))
            _account["locked_until"] = now + delay
            log.warning("admin login locked for %ss after %s failures (ip %s)", delay, _account["failures"], client_ip)
            if _account["alerted_until"] < now:
                _account["alerted_until"] = now + LOCK_MAX
                alert = delay
    if alert is not None:
        _alert_lockout(alert, client_ip)


def reset_lockout() -> None:
    """For tests."""
    with _lock:
        _account.update(failures=0, locked_until=0.0, last_failure=0.0, alerted_until=0.0)
        _failed_ip.clear()
        _mcp_failures.clear()


# --- per-IP limit ----------------------------------------------------------------------

def _too_many(client_ip: str) -> bool:
    now = time.time()
    with _lock:
        attempts = [t for t in _failed_ip.get(client_ip, []) if now - t < IP_WINDOW]
        _failed_ip[client_ip] = attempts
        return len(attempts) >= IP_MAX_ATTEMPTS


def _record(client_ip: str, ok: bool) -> None:
    with _lock:
        if not ok:
            _failed_ip.setdefault(client_ip, []).append(time.time())
        else:
            _failed_ip.pop(client_ip, None)
    _account_record(ok, client_ip)


def blocked(client_ip: str) -> bool:
    """True while no credential may be checked (account locked or this address over its limit)."""
    return _account_blocked() or _too_many(client_ip)


def check_password(username: str, password: str, client_ip: str) -> bool:
    """Step 1: user name and password."""
    if blocked(client_ip):
        return False
    s = get_settings()
    ok = hmac.compare_digest(username.encode(), s.admin_user.encode())
    try:
        ok = _ph.verify(s.admin_password_hash, password) and ok
    except (VerifyMismatchError, ValueError):
        ok = False
    _record(client_ip, ok)
    return ok


def check_totp(code: str, client_ip: str) -> bool:
    """Step 2: one-time code (only when a TOTP secret is configured)."""
    if blocked(client_ip):
        return False
    s = get_settings()
    ok = bool(s.admin_totp_secret) and pyotp.TOTP(s.admin_totp_secret).verify((code or "").replace(" ", ""), valid_window=1)
    _record(client_ip, ok)
    return ok


def totp_required() -> bool:
    return bool(get_settings().admin_totp_secret)


def create_pre_session() -> str:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="pre").dumps({"u": get_settings().admin_user, "n": secrets.token_urlsafe(8)})


def read_pre_session(request: Request) -> dict | None:
    raw = request.cookies.get(PRE_COOKIE)
    if not raw:
        return None
    try:
        return URLSafeTimedSerializer(get_settings().secret_key, salt="pre").loads(raw, max_age=PRE_MAX_AGE)
    except BadSignature:
        return None


def check_login(username: str, password: str, totp: str, client_ip: str) -> bool:
    """Single-step check (used by tests / API clients): password and, if configured, TOTP together."""
    if not check_password(username, password, client_ip):
        return False
    return check_totp(totp, client_ip) if totp_required() else True


# --- MCP bearer throttle ---------------------------------------------------------------

def mcp_failed() -> None:
    """Record a request with a wrong or missing bearer token."""
    now = time.time()
    with _lock:
        _mcp_failures[:] = [t for t in _mcp_failures if now - t < MCP_FAIL_WINDOW]
        _mcp_failures.append(now)
        if len(_mcp_failures) == MCP_FAIL_LIMIT:
            log.warning("MCP: %s wrong bearer tokens within %ss, throttling", MCP_FAIL_LIMIT, MCP_FAIL_WINDOW)


def mcp_blocked() -> bool:
    """True when unauthenticated MCP requests should be answered with 429 (valid tokens still pass)."""
    now = time.time()
    with _lock:
        _mcp_failures[:] = [t for t in _mcp_failures if now - t < MCP_FAIL_WINDOW]
        return len(_mcp_failures) >= MCP_FAIL_LIMIT


def require_login(request: Request) -> dict:
    sess = read_session(request)
    if not sess:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return sess


async def require_csrf(request: Request, sess: dict = Depends(require_login)) -> dict:
    form = await request.form()
    token = form.get("csrf", "")
    if not hmac.compare_digest(str(token), sess.get("csrf", "")):
        raise HTTPException(status_code=403, detail="CSRF-Token ungültig")
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    base = get_settings().public_base_url.rstrip("/")
    if origin and not (origin.startswith(base) or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")):
        raise HTTPException(status_code=403, detail="Origin nicht erlaubt")
    return sess
