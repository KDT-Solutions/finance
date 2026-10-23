"""Firmenpruefung beim bexio-Login und beim Token-Refresh. bexio ist gemockt, nur Platzhalterdaten."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import crypto
from app.db import Base, SessionLocal, engine
from app.models import Tenant
from app.services import bexio as bx


class FakeBexio:
    def __init__(self):
        self.company = {"company_id": "111", "company_name": "Beispiel GmbH"}
        self.n = 0

    def post(self, url, data=None, timeout=None):
        self.n += 1
        return httpx.Response(200, json={"access_token": f"at{self.n}", "refresh_token": f"rt{self.n}", "expires_in": 3600},
                              request=httpx.Request("POST", url))

    def get(self, url, headers=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer at")
        return httpx.Response(200, json={"sub": "u", **self.company}, request=httpx.Request("GET", url))


@pytest.fixture
def env(monkeypatch):
    Base.metadata.create_all(engine)
    fake = FakeBexio()
    monkeypatch.setattr(bx.httpx, "post", fake.post)
    monkeypatch.setattr(bx.httpx, "get", fake.get)
    db = SessionLocal()
    t = Tenant(code="XYZ", name="Beispiel GmbH", zammad_group_paid="a", zammad_group_topay="b")
    db.add(t)
    db.commit()
    yield fake, db, t
    db.delete(t)
    db.commit()
    db.close()


def connect(db, t, company_id=None):
    t.bexio_refresh_token = crypto.encrypt("rt0")
    t.bexio_access_token = None
    t.bexio_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    t.bexio_company_id = company_id
    db.commit()


def test_scope_contains_company_profile():
    assert "company_profile" in bx.get_settings().bexio_scopes.split()


def test_company_problem_rules(env):
    _, _, t = env
    assert bx.company_problem(t, ("111", "  beispiel   GMBH ")) is None      # Name, ohne gespeicherte ID
    assert "Andere AG" in bx.company_problem(t, ("222", "Andere AG"))
    t.bexio_company_id = "111"
    assert bx.company_problem(t, ("111", "Umbenannt GmbH")) is None           # ID massgebend
    assert "222" in bx.company_problem(t, ("222", "Beispiel GmbH"))           # gleicher Name, andere Firma


def test_refresh_pins_matching_company(env):
    fake, db, t = env
    connect(db, t)
    c = bx.BexioClient(db, t)
    assert c._token() == "at1"
    c.close()
    assert t.bexio_company_id == "111" and t.bexio_connected


def test_refresh_with_other_company_disconnects(env):
    fake, db, t = env
    connect(db, t, "111")
    fake.company = {"company_id": "999", "company_name": "Fremd AG"}
    c = bx.BexioClient(db, t)
    with pytest.raises(bx.CompanyMismatch, match="Fremd AG"):
        c._token()
    c.close()
    assert not t.bexio_connected and t.bexio_access_token is None
    assert t.bexio_company_id == "111"          # Bindung bleibt


def test_refresh_without_company_claim_is_blocked(env):
    fake, db, t = env
    connect(db, t)
    fake.company = {}
    c = bx.BexioClient(db, t)
    with pytest.raises(bx.BexioError, match="company_profile"):
        c._token()
    c.close()
    assert t.bexio_access_token is None         # kein verwendbarer Token


def _callback(t, db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import auth
    from app.models import KV
    from app.routes import ui

    # SQLite liefert naive Zeitstempel (Postgres nicht): fuer den State-Vergleich naive Zeit verwenden
    monkeypatch.setattr(ui, "now", lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[auth.require_login] = lambda: {"u": "test", "csrf": "x"}
    state = bx.new_state(t.code)
    db.merge(KV(key=f"oauth_state:{state}", value=t.code))
    db.commit()
    r = TestClient(app).get("/auth/bexio/callback", params={"code": "c", "state": state}, follow_redirects=False)
    db.refresh(t)
    from urllib.parse import unquote
    return unquote(r.headers["location"])


def test_callback_rejects_wrong_company_and_keeps_connection(env, monkeypatch):
    fake, db, t = env
    connect(db, t, "111")
    fake.company = {"company_id": "222", "company_name": "Andere AG"}
    loc = _callback(t, db, monkeypatch)
    assert "Andere AG" in loc and "Nichts gespeichert" in loc
    assert crypto.decrypt(t.bexio_refresh_token) == "rt0" and t.bexio_company_id == "111"


def test_callback_accepts_and_pins(env, monkeypatch):
    fake, db, t = env
    monkeypatch.setattr(bx.BexioClient, "refresh_master_data", lambda self: {})
    loc = _callback(t, db, monkeypatch)
    assert "verbunden" in loc
    assert t.bexio_company_id == "111" and crypto.decrypt(t.bexio_refresh_token) == "rt1"
