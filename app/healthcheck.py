"""Health check for the worker container (no HTTP server): a heartbeat file is touched after
every loop pass; the check fails when it is missing or older than WORKER_HEALTH_MAX_AGE seconds."""

from __future__ import annotations

import os
import sys
import time

HEARTBEAT = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/finance-worker.heartbeat")


def max_age() -> int:
    try:
        return max(120, int(os.environ.get("WORKER_HEALTH_MAX_AGE", "1800")))
    except ValueError:
        return 1800


def beat(path: str = HEARTBEAT) -> None:
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def check(path: str = HEARTBEAT, now: float | None = None) -> tuple[bool, str]:
    try:
        age = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        return False, f"kein Heartbeat ({path})"
    if age > max_age():
        return False, f"letzter Durchlauf vor {int(age)}s (Limit {max_age()}s)"
    return True, f"letzter Durchlauf vor {int(age)}s"


if __name__ == "__main__":
    ok, msg = check()
    print(msg)
    sys.exit(0 if ok else 1)
