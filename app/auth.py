"""Single-admin login (argon2 password + optional TOTP), signed session cookie, CSRF token."""

from __future__ import annotations

import hmac
import secrets
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

COOKIE = "finance_session"
SESSION_MAX_AGE = 12 * 3600
_failed: dict[str, list[float]] = {}
_ph = PasswordHasher()


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


def _too_many(client_ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed.get(client_ip, []) if now - t < 900]
    _failed[client_ip] = attempts
    return len(attempts) >= 5


def check_login(username: str, password: str, totp: str, client_ip: str) -> bool:
    if _too_many(client_ip):
        return False
    s = get_settings()
    ok = hmac.compare_digest(username.encode(), s.admin_user.encode())
    try:
        ok = _ph.verify(s.admin_password_hash, password) and ok
    except (VerifyMismatchError, ValueError):
        ok = False
    if ok and s.admin_totp_secret:
        ok = pyotp.TOTP(s.admin_totp_secret).verify(totp.replace(" ", ""), valid_window=1)
    if not ok:
        _failed.setdefault(client_ip, []).append(time.time())
    else:
        _failed.pop(client_ip, None)
    return ok


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
