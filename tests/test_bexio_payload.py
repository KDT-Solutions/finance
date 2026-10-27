from app.services import bexio as bx

# required properties of POST /4.0/purchase/bills per the bexio OpenAPI spec
REQUIRED = {"supplier_id", "contact_partner_id", "currency_code", "address", "bill_date", "due_date", "manual_amount",
            "item_net", "line_items", "discounts", "attachment_ids"}
PAYMENT_REQUIRED = {"type", "execution_date", "amount", "salary_payment"}


def test_bill_payload_matches_spec():
    qr = {"iban": "CH44 3199 9123 0008 8901 2", "creditor_name": "Muster AG", "reference_type": "QRR",
          "reference": "21 00000 00003 13947 14300 09017", "message": "", "billing_info": "//S1/10/123/11/240101", "is_qr_iban": True}
    p = bx.build_bill_payload(supplier_id=12, contact_partner_id=3, supplier_name="Muster AG", document_no="R-1", title="R-1",
                              bill_date="2026-10-01", due_date="2026-10-31", currency="CHF", amount_gross=107.7, account_id=99,
                              tax_id=5, text="Hosting", attachment_ids=["e84b9fe2-3fe2-4fcf-8c30-298fe16adb14"], qr=qr, bank_account_id=7)
    assert REQUIRED <= set(p)
    assert p["manual_amount"] is False and p["amount_calc"] == 107.7
    assert p["address"]["type"] == "COMPANY" and p["address"]["lastname_company"] == "Muster AG"
    li = p["line_items"][0]
    assert set(li) == {"position", "title", "amount", "booking_account_id", "tax_id"}
    pay = p["payment"]
    assert PAYMENT_REQUIRED <= set(pay)
    assert pay["type"] == "QR" and pay["iban"] == "CH4431999123000889012"
    assert pay["reference_no"] == "210000000003139471430009017" and len(pay["reference_no"]) <= 27
    assert pay["bank_account_id"] == 7 and pay["execution_date"] == "2026-10-31"
    assert p["qr_bill_information"].startswith("//S1/")


def test_bill_payload_without_partner_and_qr():
    p = bx.build_bill_payload(supplier_id=12, contact_partner_id=None, supplier_name="X", document_no="", title="",
                              bill_date="2026-10-01", due_date="", currency="", amount_gross=10, account_id=1,
                              tax_id=None, text="", attachment_ids=[], qr=None)
    assert "contact_partner_id" not in p and "payment" not in p and "vendor_ref" not in p
    assert p["due_date"] == "2026-10-01" and p["currency_code"] == "CHF"
    assert "tax_id" not in p["line_items"][0]


def test_bill_payload_with_positions():
    lines = [{"title": "MWST 8.1%", "amount": 32.4, "account_id": None, "tax_id": 35},
             {"title": "MWST 2.6%", "amount": 22.33, "account_id": 171, "tax_id": 34}]
    p = bx.build_bill_payload(supplier_id=12, contact_partner_id=3, supplier_name="Brack AG", document_no="49777632", title="",
                              bill_date="2026-10-02", due_date="2026-10-12", currency="CHF", amount_gross=54.75, account_id=170,
                              tax_id=35, text="Einkauf", attachment_ids=[], qr=None, lines=lines)
    assert p["amount_calc"] == 54.75
    assert [(li["position"], li["title"], li["amount"], li["booking_account_id"], li["tax_id"]) for li in p["line_items"]] == [
        (0, "MWST 8.1%", 32.4, 170, 35), (1, "MWST 2.6%", 22.33, 171, 34)]


def test_manual_entry_split():
    lines = [{"title": "MWST 8.1%", "amount": 32.4, "tax_id": 35}, {"title": "MWST 2.6%", "amount": 22.33, "tax_id": 34}]
    e = bx.build_manual_entry_split(lines=lines, debit_account_id=170, credit_account_id=99, description="Brack 49777632")
    assert [(x["debit_account_id"], x["credit_account_id"], x["amount"], x["tax_id"], x["tax_account_id"]) for x in e] == [
        (170, 99, 32.4, 35, 170), (170, 99, 22.33, 34, 170)]
    assert e[1]["description"] == "Brack 49777632 MWST 2.6%"



def test_customs_bill_is_net_like_the_manual_booking():
    # DHL DBX2343262 as booked by hand in bexio: 22.46 VM81 (+1.82) and 173.00 ZOLLM, item_net, total 197.28
    lines = [{"title": "Einfuhrsteuer", "amount": 173.00, "net": 173.00, "account_id": None, "tax_id": 9},
             {"title": "Zusatzleistungen", "amount": 24.28, "net": 22.46, "account_id": None, "tax_id": 35}]
    p = bx.build_bill_payload(supplier_id=141, contact_partner_id=1, supplier_name="DHL Express (Schweiz) AG", document_no="DBX2343262", title="",
                              bill_date="2026-09-29", due_date="2026-10-06", currency="CHF", amount_gross=197.28, account_id=170,
                              tax_id=9, text="Verzollung", attachment_ids=[], qr=None, lines=lines)
    assert p["item_net"] is True and p["amount_calc"] == 197.28
    assert [(li["amount"], li["tax_id"], li["booking_account_id"]) for li in p["line_items"]] == [(173.00, 9, 170), (22.46, 35, 170)]



def test_invoice_payload_for_commission():
    p = bx.build_invoice_payload(contact_id=20, user_id=1, title="Gutschrift 4643983", doc_date="2026-09-30", due_date="2026-10-30",
                                 amount_gross=26.1, account_id=289, tax_id=28, text="Provision iWay 4643983", reference="4643983")
    assert (p["mwst_type"], p["mwst_is_net"], p["contact_id"], p["user_id"]) == (0, False, 20, 1)
    assert p["positions"] == [{"type": "KbPositionCustom", "amount": "1", "unit_price": "26.10", "account_id": 289, "tax_id": 28,
                               "text": "Provision iWay 4643983"}]
    assert (p["is_valid_from"], p["is_valid_to"], p["reference"]) == ("2026-09-30", "2026-10-30", "4643983")
