from __future__ import annotations

import logging

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


def pushover(title: str, message: str) -> bool:
    s = get_settings()
    if not (s.pushover_app_token and s.pushover_user_key):
        return False
    try:
        r = httpx.post("https://api.pushover.net/1/messages.json", data={
            "token": s.pushover_app_token, "user": s.pushover_user_key, "title": title, "message": message,
        }, timeout=20)
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        log.exception("pushover failed")
        return False
