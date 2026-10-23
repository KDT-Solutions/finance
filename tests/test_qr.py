from app.services.qr import parse_billing_info, parse_spc

SPC = "\n".join([
    "SPC", "0200", "1", "CH4431999123000889012", "S", "Max Muster & Söhne", "Musterstrasse", "123", "8000", "Seldwyla", "CH",
    "", "", "", "", "", "", "",
    "1949.75", "CHF",
    "S", "Simon Muster", "Musterstrasse", "1", "8000", "Seldwyla", "CH",
    "QRR", "210000000003139471430009017", "Bestellung vom 15.10.2020", "EPD",
    "//S1/10/10201409/11/201021/30/102673831/31/201021201121/32/7.7/40/2:10;0:30",
])


def test_parse_spc():
    qr = parse_spc(SPC)
    assert qr is not None
    assert qr.iban == "CH4431999123000889012"
    assert qr.is_qr_iban
    assert qr.creditor_name == "Max Muster & Söhne"
    assert qr.amount == 1949.75
    assert qr.currency == "CHF"
    assert qr.reference_type == "QRR"
    assert qr.reference.startswith("2100000000")
    assert qr.message.startswith("Bestellung")
    b = parse_billing_info(qr.billing_info)
    assert b["document_number"] == "10201409"
    assert b["document_date"] == "2020-10-21"
    assert b["uid"] == "CHE-102.673.831"


def test_parse_spc_rejects_other():
    assert parse_spc("hello world") is None
