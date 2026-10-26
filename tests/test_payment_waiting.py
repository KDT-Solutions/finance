"""Zahlfile -> Ticket auf Warten (pending reminder), Abgleich mit bexio, Abmelden.
Zammad und bexio sind gemockt, nur Platzhalterdaten."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Booking, Document, Tenant
from app.services import bexio as bx, payments, pipeline
from app.services.zammad import PENDING_REMINDER

IBAN = "CH9300762011623852957"  # Beispiel-IBAN aus der IBAN-Spezifikation


class FakeZammad:
    def __init__(self):
        self.notes: list[tuple[int, str]] = []
        self.states: dict[int, tuple[str, datetime | None]] = {}

    def add_note(self, tid, body):
        self.notes.append((tid, body))

    def set_pending(self, tid, until):
        self.states[tid] = (PENDING_REMINDER, until)

    def set_state(self, tid, state):
        self.states[tid] = (state, None)

    def ticket_state(self, tid):
        return self.states.get(tid, ("open", None))

    def notes_for(self, tid):
        return [b for t, b in self.notes if t == tid]


class FakeBexio:
    status: dict[str, str] = {}
    calls: list[str] = []

    def __init__(self, db, tenant):
        self.tenant = tenant

    def get_bill(self, bill_id):
        FakeBexio.calls.append(f"{self.tenant.code}:{bill_id}")
        return {"id": bill_id, "status": FakeBexio.status.get(bill_id, "booked")}

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    monkeypatch.setattr(bx, "BexioClient", FakeBexio)
    FakeBexio.status, FakeBexio.calls = {}, []
    tenants = {}
    for code in ("AAA", "BBB"):
        t = Tenant(code=code, name=f"Beispiel {code} GmbH", zammad_group_paid=f"Finance {code}", zammad_group_topay=f"Finance toPay {code}",
                   payment_iban=IBAN, payment_name=f"Beispiel {code} GmbH")
        db.add(t)
        tenants[code] = t
    db.flush()
    return db, FakeZammad(), tenants


def _bill(db, tenant, tid, bill_id, *, payment_file=None, wait_until=None):
    d = Document(tenant_id=tenant.id, sha256=f"sha{tid}", filename="rechnung.pdf", storage_path="/dev/null", size=1,
                 zammad_ticket_id=tid, zammad_ticket_number=str(tid), state="booked",
                 decision={"amount": 10.0, "currency": "CHF", "document_number": f"R-{tid}", "supplier_name": "Lieferant AG"})
    db.add(d)
    db.flush()
    d.extraction = None
    b = Booking(document_id=d.id, tenant_id=tenant.id, kind="bill_open", bexio_bill_id=bill_id, amount=10.0, currency="CHF", status="ok",
                payment_file=payment_file, payment_file_at=datetime.now(timezone.utc) if payment_file else None, ticket_wait_until=wait_until)
    db.add(b)
    db.commit()
    return b


def _payable(monkeypatch):
    """payment_info ohne Extraktion: feste, gültige Zahlungsdaten."""
    monkeypatch.setattr(payments, "payment_info", lambda doc: {
        "iban": IBAN, "reference_type": "", "reference": "", "message": "Rechnung", "creditor_name": "Lieferant AG",
        "country": "CH", "street": "Musterstrasse 1", "postcode": "8000", "town": "Zürich", "problems": []})


def test_wait_until_is_14_days_at_9_local():
    until = payments.wait_until(date(2026, 10, 26))
    local = until.astimezone(ZoneInfo("Europe/Zurich"))
    assert (local.date(), local.hour, local.minute) == (date(2026, 11, 9), 9, 0)


def test_payment_file_sets_ticket_waiting(env, monkeypatch):
    db, z, t = env
    _payable(monkeypatch)
    b = _bill(db, t["AAA"], 101, "bill-1")
    name, _data, rows = payments.create_file(db, z, t["AAA"], [b.id], None, actor="test")
    state, until = z.ticket_state(101)
    assert state == PENDING_REMINDER
    assert until == b.ticket_wait_until
    assert until.astimezone(ZoneInfo("Europe/Zurich")).date() == payments.local_today() + timedelta(days=14)
    note = z.notes_for(101)[-1]
    assert name in note and "auf Warten bis" in note and "bleibt offen" not in note


def test_check_paid_closes_waiting_ticket(env):
    db, z, t = env
    b = _bill(db, t["AAA"], 102, "bill-2", payment_file="F1.xml", wait_until=datetime.now(timezone.utc) + timedelta(days=3))
    z.states[102] = (PENDING_REMINDER, datetime.now(timezone.utc) + timedelta(days=3))
    FakeBexio.status["bill-2"] = "paid"
    assert pipeline.check_paid(db, z, t["AAA"]) == 1
    assert z.ticket_state(102)[0] == "closed"
    assert b.paid_at is not None and b.ticket_wait_until is None
    assert "bezahlt abgeglichen" in z.notes_for(102)[-1]


def test_check_paid_rewaits_after_expiry(env):
    db, z, t = env
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    b = _bill(db, t["AAA"], 103, "bill-3", payment_file="F1.xml", wait_until=past)
    z.states[103] = (PENDING_REMINDER, past)
    assert pipeline.check_paid(db, z, t["AAA"]) == 0
    state, until = z.ticket_state(103)
    assert state == PENDING_REMINDER and until > datetime.now(timezone.utc) + timedelta(days=13)
    assert "erneut geprüft" in z.notes_for(103)[-1]
    assert b.paid_at is None


def test_check_paid_leaves_running_wait_alone(env):
    db, z, t = env
    future = datetime.now(timezone.utc) + timedelta(days=5)
    _bill(db, t["AAA"], 104, "bill-4", payment_file="F1.xml", wait_until=future)
    z.states[104] = (PENDING_REMINDER, future)
    pipeline.check_paid(db, z, t["AAA"])
    assert z.ticket_state(104) == (PENDING_REMINDER, future)
    assert z.notes_for(104) == []


def test_old_open_ticket_in_file_goes_waiting(env):
    """Altfall: Zahlfile vor dieser Regel, Ticket noch offen."""
    db, z, t = env
    b = _bill(db, t["AAA"], 105, "bill-5", payment_file="F1.xml")
    pipeline.check_paid(db, z, t["AAA"])
    assert z.ticket_state(105)[0] == PENDING_REMINDER
    assert b.ticket_wait_until is not None


def test_reopened_ticket_is_not_pushed_back(env):
    """Schon einmal vom Tool auf Warten gesetzt, danach wieder offen (neue Mail, von Hand): nicht anfassen."""
    db, z, t = env
    _bill(db, t["AAA"], 106, "bill-6", payment_file="F1.xml", wait_until=datetime.now(timezone.utc) - timedelta(days=1))
    z.states[106] = ("open", None)
    pipeline.check_paid(db, z, t["AAA"])
    assert z.ticket_state(106) == ("open", None)
    assert z.notes_for(106) == []


def test_closed_ticket_and_bill_without_file_untouched(env):
    db, z, t = env
    _bill(db, t["AAA"], 107, "bill-7", payment_file="F1.xml")
    z.states[107] = ("closed", None)
    _bill(db, t["AAA"], 108, "bill-8")  # noch in keinem Zahlfile
    pipeline.check_paid(db, z, t["AAA"])
    assert z.ticket_state(107) == ("closed", None)
    assert z.ticket_state(108) == ("open", None)
    assert z.notes == []


def test_check_paid_only_touches_own_tenant(env):
    db, z, t = env
    _bill(db, t["AAA"], 109, "bill-9", payment_file="F1.xml")
    _bill(db, t["BBB"], 110, "bill-10", payment_file="F2.xml")
    FakeBexio.status["bill-10"] = "paid"
    pipeline.check_paid(db, z, t["AAA"])
    assert FakeBexio.calls == ["AAA:bill-9"]
    assert z.ticket_state(110) == ("open", None)


def test_unmark_notes_and_reopens(env):
    db, z, t = env
    b = _bill(db, t["AAA"], 111, "bill-11", payment_file="F1.xml", wait_until=datetime.now(timezone.utc) + timedelta(days=10))
    z.states[111] = (PENDING_REMINDER, b.ticket_wait_until)
    payments.unmark(db, z, b, actor="test")
    assert b.payment_file is None and b.ticket_wait_until is None
    assert z.ticket_state(111) == ("open", None)
    assert "F1.xml zurückgezogen" in z.notes_for(111)[-1]


def test_unmark_refused_when_paid(env):
    db, z, t = env
    b = _bill(db, t["AAA"], 112, "bill-12", payment_file="F1.xml")
    FakeBexio.status["bill-12"] = "paid"
    with pytest.raises(ValueError, match="bereits bezahlt"):
        payments.unmark(db, z, b, actor="test")
    assert b.payment_file == "F1.xml"
    assert z.notes == []


def test_unmark_refused_when_bexio_unreachable(env, monkeypatch):
    db, z, t = env
    b = _bill(db, t["AAA"], 113, "bill-13", payment_file="F1.xml")

    def boom(self, bill_id):
        raise bx.BexioError(500, "down", "bill")

    monkeypatch.setattr(FakeBexio, "get_bill", boom)
    with pytest.raises(ValueError, match="nicht prüfbar"):
        payments.unmark(db, z, b, actor="test")
    assert b.payment_file == "F1.xml"
