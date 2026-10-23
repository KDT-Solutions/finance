"""Swiss QR-bill reading: rasterise PDF pages, decode QR codes, parse the SPC payload."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass
class SwissQR:
    iban: str
    creditor_name: str
    creditor_address: str
    amount: float | None
    currency: str
    debtor_name: str
    reference_type: str  # QRR | SCOR | NON
    reference: str
    message: str
    billing_info: str
    raw: str

    @property
    def is_qr_iban(self) -> bool:
        # QR-IBAN: IID 30000-31999
        digits = self.iban.replace(" ", "")
        return len(digits) >= 9 and 30000 <= int(digits[4:9]) <= 31999

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_qr_iban"] = self.is_qr_iban
        return d


def parse_spc(payload: str) -> SwissQR | None:
    """Parse a Swiss QR-bill payload (SPC/0200/1)."""
    lines = payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 20 or lines[0].strip() != "SPC":
        return None
    try:
        iban = lines[3].strip().replace(" ", "")
        cr_type = lines[4].strip()
        cr = lines[5:11]
        name = cr[0].strip()
        if cr_type == "S":
            address = f"{cr[1].strip()} {cr[2].strip()}, {cr[3].strip()} {cr[4].strip()} {cr[5].strip()}".strip()
        else:
            address = f"{cr[1].strip()}, {cr[2].strip()} {cr[5].strip()}".strip()
        amount_s = lines[18].strip()
        amount = float(amount_s) if amount_s else None
        currency = lines[19].strip()
        debtor = lines[21].strip() if len(lines) > 21 else ""
        ref_type = lines[27].strip() if len(lines) > 27 else "NON"
        reference = lines[28].strip() if len(lines) > 28 else ""
        message = lines[29].strip() if len(lines) > 29 else ""
        billing = ""
        for extra in lines[31:]:
            if extra.startswith("//S1/"):
                billing = extra.strip()
        return SwissQR(iban, name, address, amount, currency, debtor, ref_type, reference, message, billing, payload)
    except (IndexError, ValueError):
        log.warning("QR payload could not be parsed")
        return None


def parse_billing_info(billing: str) -> dict:
    """//S1/10/<doc no>/11/<date yymmdd>/30/<UID>/31/<period>/32/<vat>/40/<conditions>"""
    out: dict[str, str] = {}
    if not billing.startswith("//S1/"):
        return out
    parts = billing[5:].split("/")
    tags = {"10": "document_number", "11": "document_date", "20": "customer_ref", "30": "uid", "31": "vat_date", "32": "vat_details", "33": "vat_import", "40": "conditions"}
    for i in range(0, len(parts) - 1, 2):
        key = tags.get(parts[i])
        if key:
            out[key] = parts[i + 1]
    if out.get("document_date") and len(out["document_date"]) == 6:
        d = out["document_date"]
        out["document_date"] = f"20{d[0:2]}-{d[2:4]}-{d[4:6]}"
    if out.get("uid") and out["uid"].isdigit() and len(out["uid"]) == 9:
        u = out["uid"]
        out["uid"] = f"CHE-{u[0:3]}.{u[3:6]}.{u[6:9]}"
    return out


def render_pages(pdf_bytes: bytes, dpi: int = 200, max_pages: int = 6):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    scale = dpi / 72
    for i in range(min(len(pdf), max_pages)):
        page = pdf[i]
        yield page.render(scale=scale).to_pil()


def extract_text(pdf_bytes: bytes, max_chars: int = 6000) -> str:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(pdf_bytes)
        parts = []
        for i in range(min(len(pdf), 8)):
            parts.append(pdf[i].get_textpage().get_text_range())
        return "\n".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001
        log.exception("text extraction failed")
        return ""


def read_swiss_qr(pdf_bytes: bytes) -> SwissQR | None:
    """First Swiss QR-bill found in the PDF, or None."""
    try:
        import zxingcpp
    except ImportError:  # pragma: no cover
        log.warning("zxing-cpp not installed, QR reading disabled")
        return None
    try:
        for img in render_pages(pdf_bytes):
            for res in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode):
                text = res.text or ""
                if text.startswith("SPC"):
                    qr = parse_spc(text)
                    if qr:
                        return qr
    except Exception:  # noqa: BLE001
        log.exception("QR reading failed")
    return None
