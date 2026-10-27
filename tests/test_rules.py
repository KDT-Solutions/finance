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
                   master_data={"accounts": [{"id": 40, "no": "6200", "name": "Reparaturen"}, {"id": 90, "no": "4400", "name": "Einkauf DL"}, {"id": 289, "no": "3681", "name": "Provisionen"}],
                                "taxes": [{"id": 3, "code": "VB81", "rate": 8.1, "type": "pre_tax_investment", "active": True},
                                          {"id": 5, "code": "VM81", "rate": 8.1, "type": "pre_tax_material", "active": True},
                                          {"id": 6, "code": "VM26", "rate": 2.6, "type": "pre_tax_material", "active": True},
                                          {"id": 7, "code": "VIM", "rate": None, "type": "pre_tax_material", "active": True},
                                          {"id": 9, "code": "ZOLLM", "rate": 100, "type": "pre_customs_tax_material", "active": True},
                                          {"id": 11, "code": "ZOLLB", "rate": 100, "type": "pre_customs_tax_investment", "active": True},
                                          {"id": 28, "code": "UN81", "rate": 8.1, "type": "sales_tax", "active": True}]})
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


def _brack_llm():
    # Brack Rechnung 49777632: 8.1% Basis 29.97 MWST 2.43, 2.6% Basis 21.76 MWST 0.57, Total 54.75
    return {"is_receipt": True, "document_type": "invoice", "supplier_name": "Brack AG", "document_number": "49777632", "document_date": "2026-10-02",
            "currency": "CHF", "total_gross": 54.75, "total_net": 51.73, "supplier_country": "CH",
            "vat_lines": [{"rate": 2.6, "base": 21.76, "amount": 0.57}, {"rate": 8.1, "base": 29.97, "amount": 2.43}],
            "payment_status": "unpaid", "confidence": {"total_gross": 0.99, "document_date": 0.99}}


def test_decide_splits_by_vat_rate(db):
    t = db.query(Tenant).one()
    d = rules.decide(db, t, group="Finance toPay KDT", qr=None, llm=_brack_llm(), text="", supplier=None, rule=None)
    # default account 4400 (material) -> VM codes; positions 32.40 + 22.33 = 54.73, the 5-Rappen rounding to 54.75 goes to the largest
    assert d.lines == [{"title": "MWST 8.1%", "amount": 32.42, "account_id": None, "tax_id": 5},
                       {"title": "MWST 2.6%", "amount": 22.33, "account_id": None, "tax_id": 6}]
    assert "Mehrere MWST-Sätze (8.1%, 2.6%), Aufteilung prüfen" in d.reasons
    assert d.auto is False


def test_split_rounding_goes_to_largest_and_rest_without_vat(db):
    t = db.query(Tenant).one()
    llm = _brack_llm()
    lines, reasons = rules.split_by_vat(t, 90, llm, 54.78)
    assert [ln["amount"] for ln in lines] == [32.45, 22.33] and reasons == []
    lines, reasons = rules.split_by_vat(t, 90, llm, 64.73)
    assert lines[-1] == {"title": "ohne MWST", "amount": 10.0, "account_id": None, "tax_id": None}
    assert reasons == ["Positionen ohne MWST 10.00, prüfen"]


def test_single_rate_has_no_lines(db):
    t = db.query(Tenant).one()
    llm = {**_brack_llm(), "vat_lines": [{"rate": 8.1, "base": 50.65, "amount": 4.10}]}
    assert rules.split_by_vat(t, 90, llm, 54.75) == ([], [])



def _dhl_llm(duty=0.0):
    # DHL Rechnung MWST / Verzollungskosten DBX2343262: MwSt 173.00 (Code Z), Zusatzleistungen 22.46 + 1.82 (8.1%), Total 197.28
    return {"is_receipt": True, "document_type": "invoice", "supplier_name": "DHL Express (Schweiz) AG", "document_number": "DBX2343262",
            "document_date": "2026-09-29", "currency": "CHF", "total_gross": round(197.28 + duty, 2), "total_net": round(195.46 + duty, 2), "supplier_country": "CH",
            "vat_lines": [{"rate": 8.1, "base": 22.46, "amount": 1.82}, {"rate": 0, "base": 173.00, "amount": 0}],
            "customs": {"import_vat": 173.00, "customs_duty": duty, "services_net": 22.46, "services_vat": 1.82},
            "payment_status": "unpaid", "confidence": {"total_gross": 0.99, "document_date": 0.99}}


def test_decide_customs_invoice(db):
    t = db.query(Tenant).one()
    d = rules.decide(db, t, group="Finance toPay KDT", qr=None, llm=_dhl_llm(), text="", supplier=None, rule=None)
    # default account 4400 (material): ZOLLM for the import VAT, VM81 for the courier fees; not split as 0% / 8.1%
    assert d.lines == [{"title": "Einfuhrsteuer", "amount": 173.00, "net": 173.00, "account_id": None, "tax_id": 9},
                       {"title": "Zusatzleistungen", "amount": 24.28, "net": 22.46, "account_id": None, "tax_id": 5}]
    assert "Verzollungsrechnung: Einfuhrsteuer 173.00, Zusatzleistungen 24.28, Aufteilung prüfen" in d.reasons
    assert not any(r.startswith("Mehrere MWST-Sätze") for r in d.reasons)


def test_customs_on_investment_account_uses_zollb_and_duty_is_flagged(db):
    t = db.query(Tenant).one()
    lines, reasons = rules.split_customs(t, 40, _dhl_llm(duty=12.50), 209.78)  # 6200: investment / operating
    assert [(ln["title"], ln["amount"], ln["tax_id"]) for ln in lines] == [
        ("Einfuhrsteuer", 173.00, 11), ("Zoll / Abgaben", 12.50, None), ("Zusatzleistungen", 24.28, 3)]
    assert reasons == ["Verzollung: Zoll/Abgaben 12.50 ohne Steuercode, Konto und Steuer prüfen"]


def test_not_a_customs_invoice(db):
    t = db.query(Tenant).one()
    assert rules.split_customs(t, 90, _brack_llm(), 54.75) == ([], [])



def test_with_net_only_for_customs(db):
    t = db.query(Tenant).one()
    edited = [{"title": "Einfuhrsteuer", "amount": 173.00, "tax_id": 9}, {"title": "DHL", "amount": 24.28, "tax_id": 5}]
    assert [ln["net"] for ln in rules.with_net(t, edited)] == [173.00, 22.46]
    plain = [{"title": "a", "amount": 32.42, "tax_id": 5, "net": 1}, {"title": "b", "amount": 22.33, "tax_id": 6}]
    assert all("net" not in ln for ln in rules.with_net(t, plain))



def _sipcall_llm():
    # sipcall Partnerprovision September 2026: 117.55 + MwSt 8.1% 9.50 = 127.05, wird überwiesen
    return {"is_receipt": True, "document_type": "credit_note", "is_commission": True, "supplier_name": "Backbone Solutions AG",
            "document_number": "", "document_date": "2026-10-06", "currency": "CHF", "total_gross": 127.05, "total_net": 117.55,
            "supplier_country": "CH", "vat_lines": [{"rate": 8.1, "base": 117.55, "amount": 9.50}], "payment_status": "unknown",
            "confidence": {"total_gross": 0.99, "document_date": 0.99}}


def _iway_llm():
    # iWay Gutschrift 4643983: -24.15 + MwSt -1.95 = -26.10 (QR-Teil mit negativem Betrag)
    return {"is_receipt": True, "document_type": "credit_note", "is_commission": True, "supplier_name": "iWay AG",
            "document_number": "4643983", "document_date": "2026-09-30", "due_date": "2026-10-30", "currency": "CHF",
            "total_gross": -26.10, "total_net": -24.15, "supplier_country": "CH",
            "vat_lines": [{"rate": 8.1, "base": -24.15, "amount": -1.95}], "payment_status": "unpaid",
            "confidence": {"total_gross": 0.99, "document_date": 0.99}}


def test_commission_without_rule_goes_to_review(db):
    t = db.query(Tenant).one()
    t.commission_account_id = 289
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm=_sipcall_llm(), text="", supplier=None, rule=None)
    assert (d.payment_mode, d.account_id, d.tax_id, d.amount) == ("credit", 289, 28, 127.05)
    assert d.reasons == ["Provisions-Gutschrift ohne Regel, erste Buchung prüfen"] and d.auto is False
    assert d.payment_account_id is None and d.lines == []


def test_negative_credit_note_is_booked_positive(db):
    t = db.query(Tenant).one()
    t.commission_account_id = 289
    d = rules.decide(db, t, group="Finance KDT", qr={"amount": -26.10, "currency": "CHF"}, llm=_iway_llm(), text="", supplier=None, rule=None)
    assert d.amount == 26.10 and d.payment_mode == "credit" and d.tax_id == 28
    assert not any("Netto" in r or "QR-Betrag" in r or "Gutschrift, manuell" in r for r in d.reasons)


def test_commission_with_credit_rule_books_automatically(db):
    t = db.query(Tenant).one()
    sup = Supplier(tenant_id=t.id, display_name="Backbone Solutions AG", name_key=rules.normalize_name("Backbone Solutions AG"), ibans=[], name_variants=[])
    db.add(sup)
    db.flush()
    db.add(Rule(tenant_id=t.id, supplier_id=sup.id, keywords=[], account_id=40, tax_id=3, payment_mode="open", auto_book=True))
    credit = Rule(tenant_id=t.id, supplier_id=sup.id, keywords=[], account_id=289, tax_id=28, payment_mode="credit", auto_book=True)
    db.add(credit)
    db.commit()
    db.refresh(sup)
    # invoices keep their rule, commission credit notes get the credit rule
    assert rules.find_rule(sup, "").payment_mode == "open"
    assert rules.find_rule(sup, "", credit=True) is credit
    d = rules.decide(db, t, group="Finance KDT", qr=None, llm=_sipcall_llm(), text="", supplier=sup, rule=credit)
    assert d.reasons == [] and d.auto is True and (d.account_id, d.tax_id) == (289, 28)


def test_learn_credit_rule(db):
    t = db.query(Tenant).one()
    sup = db.query(Supplier).first()
    r = rules.learn_from_decision(db, t, sup, None, {"payment_mode": "credit", "account_id": 289, "tax_id": 28, "amount": 127.05})
    assert r.payment_mode == "credit" and r.payment_account_id is None and r.auto_book is True
