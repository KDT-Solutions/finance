import xml.etree.ElementTree as ET
from datetime import date, datetime

import pytest

from app.services import pain001


def test_iban_checks():
    assert pain001.iban_valid("CH93 0076 2011 6238 5295 7")
    assert pain001.iban_valid("DE61100800000221061700")
    assert not pain001.iban_valid("DE61100800000221061701")
    assert pain001.is_qr_iban("CH4431999123000889012")
    assert not pain001.is_qr_iban("CH9300762011623852957")


def test_build_file():
    pays = [
        pain001.Payment("FIN-KDT-1", 7.2, "EUR", "MWC Mobile World Communications GmbH", "DE61100800000221061700", message="Rechnung SB2026-43477",
                        country="DE", street="Musterstr. 1", postcode="10115", town="Berlin"),
        pain001.Payment("FIN-KDT-2", 150.0, "CHF", "Muster AG", "CH4431999123000889012", reference_type="QRR", reference="210000000003139471430009017", country="CH"),
    ]
    data = pain001.build(debtor_name="KDT-Solutions GmbH", debtor_iban="CH93 0076 2011 6238 5295 7", payments=pays,
                         execution_date=date(2026, 10, 24), created=datetime(2026, 10, 23, 22, 0, 0))
    root = ET.fromstring(data)
    ns = {"p": pain001.NS}
    assert root.find("p:CstmrCdtTrfInitn/p:GrpHdr/p:NbOfTxs", ns).text == "2"
    assert root.find("p:CstmrCdtTrfInitn/p:GrpHdr/p:CtrlSum", ns).text == "157.20"
    pi = root.find("p:CstmrCdtTrfInitn/p:PmtInf", ns)
    assert pi.find("p:DbtrAcct/p:Id/p:IBAN", ns).text == "CH9300762011623852957"
    assert pi.find("p:DbtrAgt/p:FinInstnId/p:ClrSysMmbId/p:MmbId", ns).text == "762"
    tx = pi.findall("p:CdtTrfTxInf", ns)
    assert tx[0].find("p:RmtInf/p:Ustrd", ns).text == "Rechnung SB2026-43477"
    assert tx[0].find("p:Cdtr/p:PstlAdr/p:TwnNm", ns).text == "Berlin"
    assert tx[0].find("p:Cdtr/p:PstlAdr/p:StrtNm", ns).text == "Musterstr. 1"
    assert tx[1].find("p:Cdtr/p:PstlAdr", ns) is None
    assert root.find("p:CstmrCdtTrfInitn/p:PmtInf/p:ReqdExctnDt/p:Dt", ns).text == "2026-10-26"  # 24.10. is a Saturday
    assert tx[0].find("p:Amt/p:InstdAmt", ns).get("Ccy") == "EUR"
    assert tx[1].find("p:RmtInf/p:Strd/p:CdtrRefInf/p:Tp/p:CdOrPrtry/p:Prtry", ns).text == "QRR"
    assert tx[1].find("p:RmtInf/p:Strd/p:CdtrRefInf/p:Ref", ns).text == "210000000003139471430009017"


def test_foreign_payment_needs_town():
    with pytest.raises(ValueError):
        pain001.build(debtor_name="X", debtor_iban="CH9300762011623852957",
                      payments=[pain001.Payment("a", 1, "EUR", "Y", "DE61100800000221061700", country="DE")])


def test_build_rejects_qr_iban_without_reference():
    with pytest.raises(ValueError):
        pain001.build(debtor_name="X", debtor_iban="CH9300762011623852957",
                      payments=[pain001.Payment("a", 1, "CHF", "Y", "CH4431999123000889012")])


def test_charge_rules():
    from app.services.pain001 import charge_rule

    assert charge_rule("DE61100800000221061700", "EUR") == ("SEPA", "SLEV")
    assert charge_rule("CH5580808009285694169", "CHF") == ("", "")
    assert charge_rule("DE61100800000221061700", "USD") == ("", "")
    assert charge_rule("HK12345678901234567890", "USD") == ("", "DEBT")
    assert charge_rule("CN12345678901234567890", "EUR") == ("", "DEBT")
