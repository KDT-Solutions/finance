"""Seite CHF-Beträge und Abschnitt in Todo: CHF-Betrag eingeben, Buchung geht an bexio (gemockt)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth
from app.config import get_settings
from app.db import Base
from app.main import app
from app.models import Document, PaymentAccount, Supplier, Tenant
from app.routes import ui
from app.services import bexio as bx

from test_paid_booking import ENTRY, FILE, FakeBexio

SESS = {"u": "test", "csrf": "tok"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(bx, "BexioClient", FakeBexio)
    monkeypatch.setattr(ui, "_zammad", lambda: None)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    FakeBexio.entry_response, FakeBexio.file_response, FakeBexio.file_error = ENTRY, [FILE], None
    FakeBexio.entries, FakeBexio.files = [], []
    db = Session()
    t = Tenant(code="AAA", name="Beispiel AAA GmbH", zammad_group_paid="Finance AAA", zammad_group_topay="Finance toPay AAA",
               master_data={"accounts": [{"id": 170, "no": "6570"}], "taxes": [{"id": 7, "code": "VIM"}]})
    db.add(t)
    db.flush()
    pa = PaymentAccount(tenant_id=t.id, label="Visa", kind="card", card_last4="3156", bexio_account_id=99)
    sup = Supplier(tenant_id=t.id, display_name="Software Ltd", name_key="software", bexio_contact_id=5, ibans=[], name_variants=[])
    db.add_all([pa, sup])
    db.flush()
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc = Document(tenant_id=t.id, sha256="sha-usd", filename="invoice.pdf", storage_path=str(pdf), size=8, state="needs_chf", supplier_id=sup.id,
                   zammad_ticket_number="100",
                   decision={"payment_mode": "card", "payment_account_id": pa.id, "account_id": 170, "tax_id": 7, "amount": 49.75, "currency": "USD",
                             "doc_date": "2026-10-26", "document_number": "INV-1", "supplier_name": "Software Ltd", "booking_text": "Abo"})
    db.add(doc)
    db.commit()

    def get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[ui.get_db] = get_db
    app.dependency_overrides[auth.require_login] = lambda: SESS
    app.dependency_overrides[auth.require_csrf] = lambda: SESS
    with TestClient(app) as c:
        yield c, Session, doc.id
    app.dependency_overrides.clear()


def test_chf_page_and_todo_list_the_document(client):
    c, _, doc_id = client
    for path in ("/chf", "/todo"):
        r = c.get(path)
        assert r.status_code == 200
        assert f'action="/docs/{doc_id}/chf"' in r.text and "USD 49.75" in r.text and "Visa ****3156" in r.text
    assert "CHF-Beträge <span class=\"badge\">1</span>" in r.text


def test_entering_chf_books(client):
    c, Session, doc_id = client
    r = c.post(f"/docs/{doc_id}/chf", data={"csrf": "tok", "amount_chf": "45.30", "back": "/todo", "tenant": "AAA"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/todo?tenant=AAA"
    doc = Session().get(Document, doc_id)
    assert doc.state == "booked" and doc.booking.amount == 45.30 and doc.booking.currency == "CHF"
    assert FakeBexio.entries[0]["entries"][0]["amount"] == 45.30


def test_missing_chf_is_rejected(client):
    c, Session, doc_id = client
    r = c.post(f"/docs/{doc_id}/chf", data={"csrf": "tok", "amount_chf": "", "back": "/chf"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert Session().get(Document, doc_id).state == "needs_chf" and FakeBexio.entries == []


def test_lists_have_open_button_and_swiss_date(client):
    c, _, doc_id = client
    for path in ("/todo", "/chf"):
        r = c.get(path)
        assert f'<a class="btn" href="/docs/{doc_id}">Öffnen</a>' in r.text
        assert "26.10.2026" in r.text and "2026-10-26</td>" not in r.text
        # the row itself is not clickable, typing into the CHF field must not navigate
        assert f'data-href="/docs/{doc_id}"' not in r.text


def _book_form(doc_id, Session, amount, lines):
    d = Session().get(Document, doc_id)
    form = {"csrf": "tok", "supplier_name": "Software Ltd", "doc_date": "2026-10-26", "amount": amount, "currency": "CHF",
            "account_no": "6570", "tax_code": "VIM", "payment_mode": "card", "payment_account_id": str(d.decision["payment_account_id"]),
            "line_title": [l[0] for l in lines], "line_amount": [l[1] for l in lines], "line_account_no": ["" for _ in lines],
            "line_tax_code": [l[2] for l in lines]}
    return form


def test_doc_page_books_positions(client, monkeypatch):
    c, Session, doc_id = client
    r = c.get(f"/docs/{doc_id}")
    assert r.text.count('name="line_amount"') == 2  # two empty rows to add positions
    form = _book_form(doc_id, Session, "54.75", [("MWST 8.1%", "32.42", "VIM"), ("MWST 2.6%", "22.33", "VIM"), ("", "", "")])
    r = c.post(f"/docs/{doc_id}/book", data=form, follow_redirects=False)
    assert r.status_code == 303
    doc = Session().get(Document, doc_id)
    assert doc.state == "booked"
    assert [ln["amount"] for ln in doc.decision["lines"]] == [32.42, 22.33]
    assert [e["amount"] for e in doc.booking.payload["entries"]] == [32.42, 22.33]


def test_doc_page_rejects_positions_not_matching_amount(client):
    c, Session, doc_id = client
    form = _book_form(doc_id, Session, "60.00", [("A", "32.42", "VIM"), ("B", "22.33", "VIM")])
    c.post(f"/docs/{doc_id}/book", data=form, follow_redirects=False)
    doc = Session().get(Document, doc_id)
    assert doc.state == "needs_review" and "Positionen ergeben 54.75 statt 60.00" in doc.review_reason
    assert FakeBexio.entries == []
