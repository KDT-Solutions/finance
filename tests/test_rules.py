import os

os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATA_ENCRYPTION_KEY", "cXr3qGq3eGf1mG0nXgq9ZzJ3Yw9j6F8lQ2bN4sT1uVw=")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_finance.db")

import pytest

from app.db import Base, SessionLocal, engine
from app.models import PaymentAccount, Rule, Supplier, Tenant
from app.services import rules


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        t = Tenant(code="KDT", name="KDT", zammad_group_paid="Finance KDT", zammad_group_topay="Finance toPay KDT", default_expense_account_id=90, default_tax_id=5,
                   master_data={"accounts": [{"id": 40, "no": "6200", "name": "Reparaturen"}, {"id": 90, "no": "4400", "name": "Einkauf DL"}],
                                "taxes": [{"id": 3, "code": "VB81", "rate": 8.1, "type": "pre_tax_investment", "active": True},
                                          {"id": 5, "code": "VM81", "rate": 8.1, "type": "pre_tax_material", "active": True},
                                          {"id": 6, "code": "VM26", "rate": 2.6, "type": "pre_tax_material", "active": True},
                                          {"id": 7, "code": "VIM", "rate": None, "type": "pre_tax_material", "active": True}]})
        s.add(t)
        s.flush()
        sup = Supplier(tenant_id=t.id, display_name="Hornbach Baumarkt AG", name_key=rules.normalize_name("Hornbach Baumarkt AG"), ibans=["CH11"], uid="CHE-111.222.333", name_variants=[])
        s.add(sup)
        s.flush()
        card = PaymentAccount(tenant_id=t.id, label="Visa", kind="card", card_last4="1234", bexio_account_id=77)
        s.add(card)
        s.flush()
        s.add(Rule(tenant_id=t.id, supplier_id=sup.id, keywords=[], account_id=40, tax_id=3, payment_mode="card", payment_account_id=card.id, auto_book=True, amount_min=10, amount_max=5000))
        s.commit()
        yield s


def test_normalize():
    assert rules.normalize_name("Hornbach Baumarkt AG") == "hornbach baumarkt"
    assert rules.normalize_name("Müller & Söhne GmbH") == "muller sohne"
    assert rules.normalize_uid("102673831") == "CHE-102.673.831"


def test_match_by_iban_uid_name(db):
    t = db.query(Tenant).one()
    assert rules.match_supplier(db, t.id, iban="CH 11")[1] == "iban"
    assert rules.match_supplier(db, t.id, uid="CHE111222333")[1] == "uid"
    assert rules.match_supplier(db, t.id, name="HORNBACH Baumarkt")[1] == "name"
    assert rules.match_supplier(db, t.id, name="Hornbach Baumarkt AG, Filiale")[1] in ("fuzzy", "name")
    assert rules.match_supplier(db, t.id, name="Migros")[0] is None


def test_decide_paid_card_auto(db):
    t = db.query(Tenant).one()
    sup, _ = rules.match_supplier(db, t.id, name="Hornbach Baumarkt AG")
    rule = rules.find_rule(sup, "Schrauben")
    llm = {"is_receipt": True, "document_type": "receipt", "supplier_name": "Hornbach", "document_number": "R1", "document_date": "2026-10-01",
           "currency": "CHF", "total_gross": 120.5, "total_net": 111.5, "vat_lines": [{"rate": 8.1, "amount": 9.0}], "payment_status": "paid",
           "paid_hint": "bezahlt mit Visa", "card_last4": "1234", "confidence": {"total_gross": 0.99, "document_date": 0.98, "supplier_name": 0.9, "payment_status": 0.9}}
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm=llm, text="Schrauben", supplier=sup, rule=rule)
    assert d.reasons == []
    assert d.auto is True
    assert d.payment_mode == "card"
    assert d.account_id == 40 and d.tax_id == 3
    assert d.move_to_topay is False


def test_decide_open_invoice_moves_to_topay(db):
    t = db.query(Tenant).one()
    qr = {"iban": "CH99", "creditor_name": "Neue Firma AG", "amount": 500.0, "currency": "CHF", "reference_type": "QRR", "reference": "1", "billing": {}}
    llm = {"is_receipt": True, "document_type": "invoice", "supplier_name": "Neue Firma AG", "document_number": "F-1", "document_date": "2026-10-02",
           "currency": "CHF", "total_gross": 500.0, "payment_status": "unpaid", "confidence": {"total_gross": 0.95, "document_date": 0.95, "supplier_name": 0.9, "payment_status": 0.9}}
    d = rules.decide(db, t, group="Finance KDT", qr=qr, llm=llm, text="", supplier=None, rule=None)
    assert d.payment_mode == "open"
    assert d.move_to_topay is True
    assert d.auto is False
    assert any("Neuer Lieferant" in r for r in d.reasons)
    assert d.account_id == 90  # tenant default proposed


def test_decide_amount_mismatch_and_topay(db):
    t = db.query(Tenant).one()
    sup, _ = rules.match_supplier(db, t.id, name="Hornbach Baumarkt AG")
    rule = rules.find_rule(sup, "")
    qr = {"iban": "CH11", "creditor_name": "Hornbach", "amount": 100.0, "currency": "CHF", "reference_type": "NON", "reference": "", "billing": {}}
    llm = {"is_receipt": True, "document_type": "invoice", "supplier_name": "Hornbach", "document_number": "X", "document_date": "2026-10-02",
           "currency": "CHF", "total_gross": 99.0, "payment_status": "unpaid", "confidence": {"total_gross": 0.95, "document_date": 0.95, "supplier_name": 0.9, "payment_status": 0.9}}
    d = rules.decide(db, t, group="Finance toPay KDT", qr=qr, llm=llm, text="", supplier=sup, rule=rule)
    assert d.payment_mode == "open"
    assert any("weicht" in r for r in d.reasons)
    assert d.auto is False


def test_learn_from_decision(db):
    t = db.query(Tenant).one()
    sup = Supplier(tenant_id=t.id, display_name="Neu AG", name_key="neu", ibans=[], name_variants=[])
    db.add(sup)
    db.flush()
    r = rules.learn_from_decision(db, t, sup, None, {"account_id": 41, "tax_id": 3, "payment_mode": "open", "amount": 200.0})
    assert r.auto_book is True and r.amount_min == 100.0 and r.amount_max == 400.0
    r2 = rules.learn_from_decision(db, t, sup, r, {"account_id": 42, "tax_id": 3, "payment_mode": "open", "amount": 300.0})
    assert r2.auto_book is False  # correction resets auto


def test_tax_from_document_not_rule(db):
    t = db.query(Tenant).one()
    sup, _ = rules.match_supplier(db, t.id, name="Hornbach Baumarkt AG")
    rule = rules.find_rule(sup, "")
    base = {"is_receipt": True, "document_type": "receipt", "supplier_name": "Hornbach", "document_number": "R2", "document_date": "2026-10-01",
            "currency": "CHF", "total_gross": 102.6, "payment_status": "paid", "paid_hint": "bezahlt", "card_last4": "1234",
            "confidence": {"total_gross": 0.99, "document_date": 0.98, "supplier_name": 0.9, "payment_status": 0.9}}
    # 2.6% on the document although the rule says VB81: code follows the document, flagged for review
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm={**base, "total_net": 100.0, "vat_lines": [{"rate": 2.6, "amount": 2.6, "base": 100.0}]}, text="", supplier=sup, rule=rule)
    assert d.tax_id == 6 and any("2.6%" in r for r in d.reasons) and d.auto is False
    # foreign supplier without CH VAT number: VIM, no tax reason
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm={**base, "supplier_country": "DE", "total_net": 102.6, "vat_lines": []}, text="", supplier=sup, rule=None)
    assert d.tax_id == 7 and not any("Steuercode" in r for r in d.reasons)
    # foreign supplier with CHE number and 8.1% on the document: domestic logic
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm={**base, "supplier_country": "LU", "supplier_uid": "CHE-123.456.789", "total_net": 100.0, "vat_lines": [{"rate": 8.1, "amount": 8.1, "base": 100.0}]}, text="", supplier=sup, rule=None)
    assert d.tax_id == 5
    # CH supplier without VAT: no code, review
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm={**base, "supplier_country": "CH", "total_net": 102.6, "vat_lines": []}, text="", supplier=sup, rule=rule)
    assert d.tax_id is None and any("Keine MWST" in r for r in d.reasons)
    # 4xxx account gets the VM code
    assert rules.pick_tax(t, 90, 8.1)["code"] == "VM81"
    assert rules.pick_tax(t, 40, 8.1)["code"] == "VB81"


def _usd_card_llm():
    return {"is_receipt": True, "document_type": "receipt", "supplier_name": "Hornbach", "document_number": "R2", "document_date": "2026-10-26",
            "currency": "USD", "total_gross": 49.75, "supplier_country": "US", "payment_status": "paid", "paid_hint": "Paid with card", "card_last4": "1234",
            "confidence": {"total_gross": 0.99, "document_date": 0.98}}


def test_decide_paid_foreign_currency_waits_for_chf(db):
    t = db.query(Tenant).one()
    sup, _ = rules.match_supplier(db, t.id, name="Hornbach Baumarkt AG")
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm=_usd_card_llm(), text="", supplier=sup, rule=rules.find_rule(sup, ""))
    assert d.reasons == [] and d.auto is True
    assert d.needs_chf is True and d.payment_mode == "card" and d.currency == "USD"


def test_decide_open_foreign_currency_stays_manual(db):
    t = db.query(Tenant).one()
    llm = {**_usd_card_llm(), "document_type": "invoice", "payment_status": "unpaid", "paid_hint": "", "card_last4": ""}
    d = rules.decide(db, t, group="Finance toPay KDT", qr=None, llm=llm, text="", supplier=None, rule=None)
    assert d.needs_chf is False
    assert "Fremdwährung USD (Phase 1 manuell)" in d.reasons
