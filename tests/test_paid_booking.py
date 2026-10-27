"""Bezahlter Beleg (card/private/bank_paid): manuelle Buchung gegen Zahlkonto.
bexio und Zammad sind gemockt, nur Platzhalterdaten."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import Document, PaymentAccount, Rule, Supplier, Tenant
from app.services import actions, bexio as bx


class FakeBexio:
    entry_response: object = None
    file_response: object = None
    file_error: Exception | None = None
    entries: list[dict] = []
    files: list[tuple[int, int]] = []

    def __init__(self, db, tenant):
        self.tenant = tenant

    def create_manual_entry(self, date, reference_nr, entries):
        FakeBexio.entries.append({"date": date, "reference_nr": reference_nr, "entries": entries})
        return FakeBexio.entry_response

    def attach_file_to_entry(self, manual_entry_id, entry_id, filename, content):
        FakeBexio.files.append((manual_entry_id, entry_id))
        if FakeBexio.file_error:
            raise FakeBexio.file_error
        return FakeBexio.file_response

    def create_invoice(self, payload):
        FakeBexio.invoices.append(payload)
        return {"id": 1720, "document_nr": "RE-01720", "kb_item_status_id": 7}

    def search_contacts(self, name):
        return []

    def create_contact(self, name):
        FakeBexio.contacts.append(name)
        return {"id": 999}

    def upload_file(self, filename, content):
        FakeBexio.uploads.append(filename)
        return [{"id": 77, "uuid": "f-uuid-77", "name": filename}]

    def add_invoice_comment(self, invoice_id, text, user_id, user_name):
        FakeBexio.comments.append((invoice_id, text, user_id, user_name))
        return {"id": 1}

    def issue_invoice(self, invoice_id):
        FakeBexio.issued.append(invoice_id)
        return {"success": True}

    def me(self):
        return {"id": 1, "firstname": "Kim", "lastname": "Thurnherr"}

    def close(self):
        pass


ENTRY = {"id": 1146, "type": "manual_single_entry", "date": "2026-10-26", "reference_nr": "R-1",
         "entries": [{"id": 21921, "debit_account_id": 170, "credit_account_id": 99, "amount": 12.97}]}
FILE = {"id": 5, "uuid": "11111111-2222-3333-4444-555555555555", "name": "beleg.pdf"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    monkeypatch.setattr(bx, "BexioClient", FakeBexio)
    FakeBexio.entry_response, FakeBexio.file_response, FakeBexio.file_error = ENTRY, [FILE], None
    FakeBexio.entries, FakeBexio.files = [], []
    FakeBexio.invoices, FakeBexio.issued = [], []
    FakeBexio.uploads, FakeBexio.comments, FakeBexio.contacts = [], [], []
    t = Tenant(code="AAA", name="Beispiel AAA GmbH", zammad_group_paid="Finance AAA", zammad_group_topay="Finance toPay AAA",
               master_data={"accounts": [{"id": 170, "no": "4200"}], "taxes": [{"id": 7, "code": "VIM"}]})
    db.add(t)
    db.flush()
    pa = PaymentAccount(tenant_id=t.id, label="Visa", kind="card", bexio_account_id=99)
    sup = Supplier(tenant_id=t.id, display_name="Lieferant AG", name_key="lieferant ag", bexio_contact_id=515, ibans=[], name_variants=[])
    db.add_all([pa, sup])
    db.flush()
    pdf = tmp_path / "beleg.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc = Document(tenant_id=t.id, sha256="sha1", filename="beleg.pdf", storage_path=str(pdf), size=8, state="needs_review", supplier_id=sup.id)
    db.add(doc)
    db.commit()
    dec = {"payment_mode": "card", "payment_account_id": pa.id, "account_id": 170, "tax_id": 7, "amount": 12.97, "currency": "CHF",
           "doc_date": "2026-10-26", "document_number": "R-1", "supplier_name": "Lieferant AG", "booking_text": "Lizenz"}
    return db, doc, dec


def test_card_booking_with_list_file_response(env):
    db, doc, dec = env
    out = actions.book_with_decision(db, None, doc, dec, remember=True, keywords=[], actor="test")
    assert out["booked"] and doc.state == "booked" and doc.last_error is None
    b = doc.booking
    assert (b.kind, b.bexio_entry_id, b.bexio_file_id) == ("entry", 1146, FILE["uuid"])
    assert FakeBexio.files == [(1146, 21921)]
    line = FakeBexio.entries[0]["entries"][0]
    assert (line["debit_account_id"], line["credit_account_id"], line["tax_id"]) == (170, 99, 7)
    rule = db.get(Rule, out["rule_id"])
    assert (rule.payment_mode, rule.tax_id, rule.account_id) == ("card", 7, 170)


def test_card_booking_with_list_entry_response(env):
    db, doc, dec = env
    FakeBexio.entry_response, FakeBexio.file_response = [ENTRY], FILE
    out = actions.book_with_decision(db, None, doc, dec, remember=False, keywords=[], actor="test")
    assert out["booked"] and out["rule_id"] is None
    assert (doc.booking.bexio_entry_id, doc.booking.bexio_file_id) == (1146, FILE["uuid"])


def test_card_booking_survives_failed_attach(env):
    """Die Buchung existiert in bexio, sobald create_manual_entry durch ist: ein Fehler beim Anhang darf sie nicht verlieren."""
    db, doc, dec = env
    FakeBexio.file_error = bx.BexioError(500, "boom", "files")
    out = actions.book_with_decision(db, None, doc, dec, remember=False, keywords=[], actor="test")
    assert out["booked"] and doc.booking.bexio_entry_id == 1146 and doc.booking.bexio_file_id is None


def test_failed_booking_writes_no_rule(env, monkeypatch):
    db, doc, dec = env

    def fail(self, *a, **kw):
        raise bx.BexioError(422, "invalid", "manual_entries")

    monkeypatch.setattr(FakeBexio, "create_manual_entry", fail)
    out = actions.book_with_decision(db, None, doc, dec, remember=True, keywords=[], actor="test")
    assert out == {"booked": False, "rule_id": None}
    assert doc.state == "error" and db.query(Rule).count() == 0


def test_foreign_card_booked_in_chf(env):
    db, doc, dec = env
    doc.state = "needs_chf"
    doc.decision = {**dec, "currency": "USD", "amount": 49.75}
    db.commit()
    out = actions.book_chf(db, None, doc, 45.30, actor="test")
    assert out["booked"] and doc.state == "booked"
    line = FakeBexio.entries[0]["entries"][0]
    assert line["amount"] == 45.30 and line["currency_id"] == 1
    assert "(USD 49.75)" in line["description"]
    assert (doc.booking.amount, doc.booking.currency) == (45.30, "CHF")
    assert doc.decision["amount_chf"] == 45.30


def test_foreign_card_without_chf_is_not_booked(env):
    db, doc, dec = env
    out = actions.book_with_decision(db, None, doc, {**dec, "currency": "USD"}, remember=False, keywords=[], actor="test")
    assert not out["booked"] and doc.state == "error" and "CHF-Betrag fehlt" in doc.last_error
    assert FakeBexio.entries == []


def test_foreign_card_document_lands_in_needs_chf(env, monkeypatch):
    from app.services import pipeline

    db, doc, dec = env
    sup = doc.supplier
    db.add(Rule(tenant_id=doc.tenant_id, supplier_id=sup.id, keywords=[], account_id=170, tax_id=7, payment_mode="card",
                payment_account_id=dec["payment_account_id"], auto_book=True))
    db.commit()
    llm = {"is_receipt": True, "document_type": "invoice", "supplier_name": "Lieferant AG", "document_number": "R-1", "document_date": "2026-10-26",
           "currency": "USD", "total_gross": 49.75, "supplier_country": "US", "payment_status": "paid", "paid_hint": "Paid",
           "confidence": {"total_gross": 0.99, "document_date": 0.99}}
    monkeypatch.setattr(pipeline.extract, "extract", lambda pdf, ctx: (llm, "test"))
    monkeypatch.setattr(pipeline.qrmod, "read_swiss_qr", lambda pdf: None)
    monkeypatch.setattr(pipeline.qrmod, "extract_text", lambda pdf: "")
    pipeline.process_document(db, None, doc)
    assert doc.state == "needs_chf" and FakeBexio.entries == []
    assert doc.review_reason == "CHF-Betrag fehlt (USD 49.75 bezahlt)"


LINES = [{"title": "MWST 8.1%", "amount": 32.40, "account_id": None, "tax_id": 7}, {"title": "MWST 2.6%", "amount": 22.33, "account_id": None, "tax_id": 8}]


def test_card_booking_with_positions_is_compound(env, monkeypatch):
    db, doc, dec = env
    sent = {}
    orig = FakeBexio.create_manual_entry

    def capture(self, date, reference_nr, entries):
        sent["entries"] = entries
        return orig(self, date, reference_nr, entries)

    monkeypatch.setattr(FakeBexio, "create_manual_entry", capture)
    out = actions.book_with_decision(db, None, doc, {**dec, "amount": 54.73, "lines": LINES}, remember=False, keywords=[], actor="test")
    assert out["booked"]
    assert [(e["amount"], e["tax_id"], e["debit_account_id"]) for e in sent["entries"]] == [(32.40, 7, 170), (22.33, 8, 170)]


def test_positions_must_add_up(env):
    db, doc, dec = env
    out = actions.book_with_decision(db, None, doc, {**dec, "amount": 60.00, "lines": LINES}, remember=False, keywords=[], actor="test")
    assert not out["booked"] and "Positionen ergeben 54.73, Betrag ist 60.00" in doc.last_error
    assert FakeBexio.entries == []



def test_commission_credit_note_becomes_debtor_invoice(env):
    db, doc, dec = env
    credit = {**dec, "payment_mode": "credit", "payment_account_id": None, "amount": 127.05, "document_number": "",
              "booking_text": "Partnerprovision September 2026"}
    out = actions.book_with_decision(db, None, doc, credit, remember=True, keywords=[], actor="test")
    assert out["booked"] and doc.state == "booked"
    inv = FakeBexio.invoices[0]
    assert inv["contact_id"] == 515 and inv["positions"][0]["unit_price"] == "127.05" and inv["positions"][0]["account_id"] == 170
    assert FakeBexio.issued == [1720] and FakeBexio.entries == []
    b = doc.booking
    assert (b.kind, b.bexio_bill_id, b.bexio_status) == ("credit_invoice", "1720", "pending")
    rule = db.get(Rule, out["rule_id"])
    assert rule.payment_mode == "credit" and rule.account_id == 170
    # the API cannot attach files to debtor invoices: PDF to bexio files, internal comment on the invoice
    assert FakeBexio.uploads == ["beleg.pdf"] and b.bexio_file_id == "f-uuid-77"
    link = f"{get_settings().public_base_url.rstrip('/')}/docs/{doc.id}/pdf"
    assert FakeBexio.comments == [(1720, f"Beleg: {link} (beleg.pdf, auch in bexio Dateien, ID f-uuid-77), Ticket#-", 1, "Kim Thurnherr")]


def _sipcall_setup(db, doc, monkeypatch, choice):
    from app.services import pipeline

    t = doc.tenant
    sip = Supplier(tenant_id=t.id, display_name="sipcall by Backbone Solutions AG", name_key="sipcall by backbone solutions",
                   ibans=[], name_variants=[], bexio_contact_id=20, street="Chaltenbodenstrasse 4a", postcode="8834", city="Schindellegi")
    db.add(sip)
    db.add(PaymentAccount(tenant_id=t.id, label="Bank", kind="bank", bexio_account_id=98))
    db.commit()
    llm = {"is_receipt": True, "document_type": "credit_note", "is_commission": True, "supplier_name": "Backbone Solutions AG (sipcall.ch)",
           "supplier_uid": "CHE-102.755.328", "supplier_street": "Chaltenbodenstrasse 4a", "supplier_postcode": "8834", "supplier_city": "Schindellegi",
           "document_date": "2026-10-06", "currency": "CHF", "total_gross": 127.05, "total_net": 117.55,
           "vat_lines": [{"rate": 8.1, "base": 117.55, "amount": 9.5}], "confidence": {"total_gross": 0.99, "document_date": 0.99}}
    seen = {}

    def choose(document, candidates):
        seen["document"], seen["candidates"] = document, candidates
        return choice(sip) if choice else None

    monkeypatch.setattr(pipeline.extract, "extract", lambda pdf, ctx: (llm, "test"))
    monkeypatch.setattr(pipeline.extract, "choose_supplier", choose)
    monkeypatch.setattr(pipeline.qrmod, "read_swiss_qr", lambda pdf: None)
    monkeypatch.setattr(pipeline.qrmod, "extract_text", lambda pdf: "")
    doc.supplier_id = None
    db.commit()
    db.refresh(doc)
    pipeline.process_document(db, None, doc)
    return sip, seen


def test_ai_assigns_known_supplier_for_review(env, monkeypatch):
    db, doc, dec = env
    doc.tenant.commission_account_id = 170
    sip, seen = _sipcall_setup(db, doc, monkeypatch, lambda s: {"supplier_id": s.id, "confidence": 0.95, "reason": "gleiche Adresse, sipcall ist Marke"})
    assert seen["candidates"][0]["id"] == sip.id  # most similar first
    assert doc.supplier_id == sip.id and doc.decision["matched_by"] == "ai" and doc.state == "needs_review"
    assert "Lieferant per KI zugeordnet: sipcall by Backbone Solutions AG (gleiche Adresse, sipcall ist Marke), prüfen" in doc.review_reason
    assert db.query(Supplier).filter(Supplier.name_key == "backbone solutions sipcall ch").count() == 0
    # nothing learned before a person confirms
    assert sip.name_variants == [] and sip.uid is None
    out = actions.book_with_decision(db, None, doc, dict(doc.decision), remember=False, keywords=[], actor="test")
    assert out["booked"] and FakeBexio.contacts == [] and FakeBexio.invoices[0]["contact_id"] == 20
    assert sip.name_variants == ["Backbone Solutions AG (sipcall.ch)"] and sip.uid == "CHE-102.755.328"


def test_ai_unsure_creates_new_supplier_as_before(env, monkeypatch):
    db, doc, dec = env
    sip, _ = _sipcall_setup(db, doc, monkeypatch, lambda s: {"supplier_id": s.id, "confidence": 0.4, "reason": "unsicher"})
    assert doc.supplier_id != sip.id and doc.decision["matched_by"] == "new"
    assert not any("KI" in r for r in doc.decision["reasons"])
