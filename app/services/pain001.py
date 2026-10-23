"""pain.001.001.09 payment file (Swiss Payment Standard) for upload in e-banking.

One PmtInf per file (one debtor account, one execution date), one CdtTrfTxInf per bill.
Payment types: QR-IBAN with QR reference (QRR), IBAN with creditor reference (SCOR) or
IBAN with free message (SEPA / foreign). The debtor agent is given as Swiss clearing
number (IID from the debtor IBAN), so no BIC needs to be configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from xml.sax.saxutils import escape

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"


@dataclass
class Payment:
    end_to_end_id: str
    amount: float
    currency: str
    creditor_name: str
    iban: str
    reference_type: str = ""  # QRR | SCOR | ""
    reference: str = ""
    message: str = ""
    country: str = ""
    address_lines: list[str] = field(default_factory=list)
    street: str = ""
    postcode: str = ""
    town: str = ""


def iban_valid(iban: str) -> bool:
    s = re.sub(r"\s+", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def is_qr_iban(iban: str) -> bool:
    s = re.sub(r"\s+", "", iban or "").upper()
    return s[:2] in ("CH", "LI") and len(s) >= 9 and 30000 <= int(s[4:9]) <= 31999


_ASCII = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2012": "-", "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00a0": " "})


def _clean(text: str, limit: int) -> str:
    """SPS character set: Latin letters, digits, common punctuation; typographic dashes and quotes mapped to ASCII."""
    t = (text or "").translate(_ASCII)
    t = re.sub(r"[\x00-\x1f\x7f]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _id(text: str, limit: int = 35) -> str:
    t = re.sub(r"[^A-Za-z0-9\-]", "-", text or "")
    return (t or "X")[:limit]


# IBAN countries of the SEPA area (EU/EEA, CH, GB, MC, SM, AD, VA and the territories with their own IBAN prefix)
SEPA_COUNTRIES = set("""AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH GB MC SM AD VA
GI GG JE IM AX GF GP MQ RE YT PM BL MF""".split())


def charge_rule(iban: str, currency: str) -> tuple[str, str]:
    """(service level, charge bearer) for a creditor IBAN:
    EUR to a SEPA country outside CH/LI -> SEPA with SLEV; any country outside the SEPA area -> charges paid by us (DEBT = OUR);
    domestic and other cases -> bank default (nothing set)."""
    country = re.sub(r"\s+", "", iban or "").upper()[:2]
    if country in ("CH", "LI"):
        return "", ""
    if country in SEPA_COUNTRIES:
        return ("SEPA", "SLEV") if (currency or "").upper() == "EUR" else ("", "")
    return "", "DEBT"


def next_bank_day(d: date) -> date:
    """Saturday/Sunday -> Monday (public holidays are adjusted by the bank itself)."""
    from datetime import timedelta

    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def build(*, debtor_name: str, debtor_iban: str, payments: list[Payment], execution_date: date | None = None,
          msg_id: str | None = None, created: datetime | None = None) -> bytes:
    if not iban_valid(debtor_iban):
        raise ValueError("Debtor IBAN invalid")
    debtor_iban = re.sub(r"\s+", "", debtor_iban).upper()
    if debtor_iban[:2] != "CH" and debtor_iban[:2] != "LI":
        raise ValueError("Debtor IBAN must be a Swiss/Liechtenstein IBAN")
    if not payments:
        raise ValueError("No payments")
    created = created or datetime.now()
    execution_date = next_bank_day(execution_date or created.date())
    msg_id = _id(msg_id or created.strftime("FIN%Y%m%d%H%M%S"))
    total = round(sum(p.amount for p in payments), 2)
    iid = str(int(debtor_iban[4:9]))

    tx = []
    for i, p in enumerate(payments, start=1):
        if not iban_valid(p.iban):
            raise ValueError(f"Creditor IBAN invalid: {p.iban} ({p.creditor_name})")
        iban = re.sub(r"\s+", "", p.iban).upper()
        qr = is_qr_iban(iban)
        if qr and p.reference_type != "QRR":
            raise ValueError(f"QR-IBAN needs a QR reference: {p.creditor_name}")
        if p.reference_type == "QRR" and not qr:
            raise ValueError(f"QR reference needs a QR-IBAN: {p.creditor_name}")
        rmt = ""
        if p.reference_type == "QRR":
            rmt = (f"<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp><Ref>{escape(_clean(p.reference, 27))}</Ref></CdtrRefInf>"
                   + (f"<AddtlRmtInf>{escape(_clean(p.message, 140))}</AddtlRmtInf>" if p.message else "") + "</Strd></RmtInf>")
        elif p.reference_type == "SCOR":
            rmt = (f"<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Cd>SCOR</Cd></CdOrPrtry></Tp><Ref>{escape(_clean(p.reference, 25))}</Ref></CdtrRefInf>"
                   + (f"<AddtlRmtInf>{escape(_clean(p.message, 140))}</AddtlRmtInf>" if p.message else "") + "</Strd></RmtInf>")
        elif p.message:
            rmt = f"<RmtInf><Ustrd>{escape(_clean(p.message, 140))}</Ustrd></RmtInf>"
        # structured postal address (StrtNm, PstCd, TwnNm, Ctry); the banks require TwnNm whenever PstlAdr is given,
        # so without a town no address is sent at all
        addr = ""
        if p.town:
            parts = [f"<StrtNm>{escape(_clean(p.street, 70))}</StrtNm>" if p.street else "",
                     f"<PstCd>{escape(_clean(p.postcode, 16))}</PstCd>" if p.postcode else "",
                     f"<TwnNm>{escape(_clean(p.town, 35))}</TwnNm>",
                     f"<Ctry>{escape(p.country[:2].upper())}</Ctry>" if p.country else ""]
            addr = "<PstlAdr>" + "".join(parts) + "</PstlAdr>"
        if not qr and not p.town and iban[:2] not in ("CH", "LI"):
            raise ValueError(f"Adresse (Ort) fehlt für Auslandszahlung: {p.creditor_name}")
        svc, chrg = charge_rule(iban, p.currency)
        if svc == "SEPA" and p.reference_type == "QRR":
            svc, chrg = "", ""
        tx.append(
            "<CdtTrfTxInf>"
            f"<PmtId><InstrId>{escape(_id(f'{msg_id}-{i}'))}</InstrId><EndToEndId>{escape(_id(p.end_to_end_id))}</EndToEndId></PmtId>"
            + (f"<PmtTpInf><SvcLvl><Cd>{svc}</Cd></SvcLvl></PmtTpInf>" if svc else "")
            + f"<Amt><InstdAmt Ccy=\"{escape(p.currency.upper())}\">{p.amount:.2f}</InstdAmt></Amt>"
            + (f"<ChrgBr>{chrg}</ChrgBr>" if chrg else "")
            + 
            f"<Cdtr><Nm>{escape(_clean(p.creditor_name, 70))}</Nm>{addr}</Cdtr>"
            f"<CdtrAcct><Id><IBAN>{iban}</IBAN></Id></CdtrAcct>"
            f"{rmt}"
            "</CdtTrfTxInf>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Document xmlns="{NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<CstmrCdtTrfInitn>"
        f"<GrpHdr><MsgId>{escape(msg_id)}</MsgId><CreDtTm>{created.strftime('%Y-%m-%dT%H:%M:%S')}</CreDtTm>"
        f"<NbOfTxs>{len(payments)}</NbOfTxs><CtrlSum>{total:.2f}</CtrlSum>"
        f"<InitgPty><Nm>{escape(_clean(debtor_name, 70))}</Nm></InitgPty></GrpHdr>"
        f"<PmtInf><PmtInfId>{escape(msg_id)}-1</PmtInfId><PmtMtd>TRF</PmtMtd><BtchBookg>false</BtchBookg>"
        f"<NbOfTxs>{len(payments)}</NbOfTxs><CtrlSum>{total:.2f}</CtrlSum>"
        f"<ReqdExctnDt><Dt>{execution_date.isoformat()}</Dt></ReqdExctnDt>"
        f"<Dbtr><Nm>{escape(_clean(debtor_name, 70))}</Nm></Dbtr>"
        f"<DbtrAcct><Id><IBAN>{debtor_iban}</IBAN></Id></DbtrAcct>"
        f"<DbtrAgt><FinInstnId><ClrSysMmbId><ClrSysId><Cd>CHBCC</Cd></ClrSysId><MmbId>{iid}</MmbId></ClrSysMmbId></FinInstnId></DbtrAgt>"
        + "".join(tx)
        + "</PmtInf></CstmrCdtTrfInitn></Document>"
    )
    return xml.encode("utf-8")
