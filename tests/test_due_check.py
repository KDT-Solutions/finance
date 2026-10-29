"""Fälligkeits-Check: Kreditoren (purchase bills) und Debitoren (kb_invoice). bexio ist gemockt, nur Platzhalterdaten."""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Booking, Document, Tenant
from app.services import bexio as bx, due_check

TODAY = date(2026, 10, 29)


class FakeBexio:
    bills: list[dict] = []
    invoices: list[dict] = []
    contacts: dict[int, dict] = {}

    def __init__(self, db, tenant):
        self.tenant = tenant

    def list_bills(self, **params):
        assert params["status"] == "TODO"
        return {"data": FakeBexio.bills}

    def get(self, path, **params):
        if path == "/2.0/kb_invoice":
            return FakeBexio.invoices[params["offset"]:params["offset"] + params["limit"]]
        if path.startswith("/2.0/contact/"):
            return FakeBexio.contacts.get(int(path.rsplit("/", 1)[1]), {})
        raise AssertionError(path)

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    monkeypatch.setattr(bx, "BexioClient", FakeBexio)
    t = Tenant(code="AAA", name="Beispiel AAA GmbH", zammad_group_paid="Finance AAA", zammad_group_topay="Finance toPay AAA",
               bexio_refresh_token="x")
    db.add(t)
    db.flush()
    FakeBexio.bills = [
        {"id": "b-over", "vendor": "Lieferant Eins AG", "vendor_ref": "R-1", "bill_date": "2026-08-31", "due_date": "2026-09-25",
         "pending_amount": 43.2, "currency_code": "CHF"},
        {"id": "b-soon", "vendor": "Lieferant Zwei AG", "vendor_ref": "R-2", "bill_date": "2026-10-20", "due_date": "2026-11-02",
         "pending_amount": 100, "currency_code": "CHF"},
        {"id": "b-later", "vendor": "Lieferant Drei AG", "vendor_ref": "R-3", "bill_date": "2026-10-20", "due_date": "2026-11-30",
         "pending_amount": 10, "currency_code": "CHF"},
        {"id": "b-paid", "vendor": "Lieferant Vier AG", "vendor_ref": "R-4", "bill_date": "2026-09-01", "due_date": "2026-09-30",
         "pending_amount": 0, "currency_code": "CHF"},
    ]
    FakeBexio.invoices = [
        {"id": 1, "document_nr": "RE-1", "contact_id": 7, "kb_item_status_id": 8, "is_valid_from": "2026-09-01", "is_valid_to": "2026-10-01",
         "total": "200.00", "total_remaining_payments": "200.00"},
        {"id": 2, "document_nr": "RE-2", "contact_id": 7, "kb_item_status_id": 9, "is_valid_from": "2026-09-01", "is_valid_to": "2026-10-01",
         "total": "50.00", "total_remaining_payments": "0.00"},
        {"id": 3, "document_nr": "RE-3", "contact_id": 8, "kb_item_status_id": 16, "is_valid_from": "2026-10-01", "is_valid_to": "2026-10-31",
         "total": "300.00", "total_remaining_payments": "120.50"},
        {"id": 4, "document_nr": "RE-4", "contact_id": 8, "kb_item_status_id": 7, "is_valid_from": "2026-09-01", "is_valid_to": "2026-09-30",
         "total": "99.00"},
    ]
    FakeBexio.contacts = {7: {"name_1": "Kunde Eins GmbH"}, 8: {"name_1": "Muster", "name_2": "Max"}}
    return db, t


def test_creditors_and_debtors(env):
    db, t = env
    res = due_check.check_tenant(db, t, days=5, today=TODAY)
    assert [i["id"] for i in res["kreditoren"]] == ["b-over", "b-soon"]
    assert res["kreditoren"][0]["days_overdue"] == 34
    assert res["kreditoren"][1]["days_overdue"] == -4
    assert [i["id"] for i in res["debitoren"]] == [1, 3]
    assert res["debitoren"][1]["open"] == 120.5
    assert res["debitoren"][1]["party"] == "Muster Max"
    assert not res["errors"]


def test_payment_file_marked_and_text(env):
    db, t = env
    d = Document(tenant_id=t.id, sha256="s", filename="r.pdf", storage_path="/dev/null", size=1, state="booked")
    db.add(d)
    db.flush()
    db.add(Booking(document_id=d.id, tenant_id=t.id, kind="bill_open", bexio_bill_id="b-soon", payment_file="pain-1.xml"))
    db.flush()
    text = due_check.summary_text(due_check.check_all(db, days=5, today=TODAY))
    assert "Lieferant Eins AG R-1: CHF 43.20, 34 T überfällig" in text
    assert "Lieferant Zwei AG R-2: CHF 100.00, fällig in 4 T (im Zahlfile)" in text
    assert "Kunde Eins GmbH: CHF 200.00, 28 T überfällig" in text
    assert "Lieferant Drei" not in text
    assert text.index("Lieferant Eins") < text.index("Lieferant Zwei")


def test_nothing_due(env):
    db, t = env
    FakeBexio.bills, FakeBexio.invoices = [], []
    assert due_check.summary_text(due_check.check_all(db, days=5, today=TODAY)) == ""
