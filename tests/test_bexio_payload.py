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
