"""Payment files: open purchase bills -> pain.001 for upload in e-banking."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Booking, Document, Tenant, now
from . import audit, pain001
from .zammad import ZammadClient

log = logging.getLogger(__name__)


def payment_info(doc: Document) -> dict:
    """What we know for paying this document: IBAN (QR part first, else document), reference, message."""
    ex = doc.extraction
    qr = (ex.qr if ex else None) or {}
    llm = (ex.llm if ex else None) or {}
    dec = doc.decision or {}
    sup = doc.supplier
    iban = (qr.get("iban") or llm.get("supplier_iban") or "").replace(" ", "").upper()
    if not iban and sup and sup.ibans:
        iban = sup.ibans[0]
    ref_type = qr.get("reference_type") if qr.get("reference_type") in ("QRR", "SCOR") else ""
    reference = (qr.get("reference") or "").replace(" ", "") if ref_type else ""
    docno = dec.get("document_number") or ""
    message = qr.get("message") or (f"Rechnung {docno}" if docno else "")
    name = qr.get("creditor_name") or (sup.display_name if sup else dec.get("supplier_name")) or ""
    country = (llm.get("supplier_country") or "").upper()
    if not country and iban:
        country = iban[:2]
    problems = []
    if not iban:
        problems.append("keine IBAN")
    elif not pain001.iban_valid(iban):
        problems.append("IBAN ungültig")
    elif pain001.is_qr_iban(iban) and ref_type != "QRR":
        problems.append("QR-IBAN ohne QR-Referenz")
    if not (dec.get("amount") or 0) > 0:
        problems.append("Betrag fehlt")
    return {"iban": iban, "reference_type": ref_type, "reference": reference, "message": message, "creditor_name": name,
            "country": country, "address": qr.get("creditor_address") or "", "problems": problems}


def open_bills(db: Session, tenant: Tenant, include_in_file: bool = False) -> list[dict]:
    """Open bills booked by the tool that are not paid and (by default) not yet in a payment file."""
    q = (db.query(Booking).join(Document).filter(Booking.tenant_id == tenant.id, Booking.kind == "bill_open", Booking.status == "ok",
                                                 Booking.paid_at.is_(None)))
    if not include_in_file:
        q = q.filter(Booking.payment_file_at.is_(None))
    out = []
    for b in q.order_by(Booking.booked_at.asc()).all():
        d = b.document
        info = payment_info(d)
        dec = d.decision or {}
        out.append({"booking_id": b.id, "document_id": d.id, "supplier": info["creditor_name"], "amount": b.amount, "currency": b.currency or "CHF",
                    "document_number": dec.get("document_number"), "due_date": dec.get("due_date"), "ticket": d.zammad_ticket_number,
                    "bexio_bill_id": b.bexio_bill_id, "payment_file": b.payment_file, "payment_file_at": b.payment_file_at, **info})
    return out


def create_file(db: Session, z: ZammadClient | None, tenant: Tenant, booking_ids: list[int], execution_date: date | None, actor: str) -> tuple[str, bytes, list[dict]]:
    """Build the pain.001 for the given bookings, store it under DATA_DIR/payments/<code>/, mark the bookings, note in tickets."""
    if not tenant.payment_iban:
        raise ValueError(f"{tenant.code}: keine Zahl-IBAN in den Einstellungen (payment_iban)")
    rows = [r for r in open_bills(db, tenant, include_in_file=True) if r["booking_id"] in set(booking_ids)]
    if not rows:
        raise ValueError("Keine passenden offenen Rechnungen")
    bad = [f"{r['supplier']} ({', '.join(r['problems'])})" for r in rows if r["problems"]]
    if bad:
        raise ValueError("Nicht zahlbar: " + "; ".join(bad))
    already = [r for r in rows if r["payment_file"]]
    if already:
        raise ValueError("Schon in einem Zahlfile: " + ", ".join(f"{r['supplier']} ({r['payment_file']})" for r in already))
    payments = [pain001.Payment(end_to_end_id=f"FIN-{tenant.code}-{r['booking_id']}", amount=float(r["amount"]), currency=r["currency"],
                                creditor_name=r["creditor_name"], iban=r["iban"], reference_type=r["reference_type"], reference=r["reference"],
                                message=r["message"], country=r["country"], address_lines=[r["address"]] if r["address"] else [])
                for r in rows]
    created = now()
    msg_id = f"FIN{tenant.code}{created.strftime('%Y%m%d%H%M%S')}"
    data = pain001.build(debtor_name=tenant.payment_name or tenant.name, debtor_iban=tenant.payment_iban, payments=payments,
                         execution_date=execution_date, msg_id=msg_id, created=created)
    name = f"{msg_id}.xml"
    folder = os.path.join(get_settings().data_dir, "payments", tenant.code)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as f:
        f.write(data)
    for r in rows:
        b = db.get(Booking, r["booking_id"])
        b.payment_file, b.payment_file_at = name, created
        if z and b.document.zammad_ticket_id:
            try:
                z.add_note(b.document.zammad_ticket_id, f"finance: Zahlung im Zahlfile {name} enthalten ({r['currency']} {float(r['amount']):.2f}, "
                                                        f"Ausführung {(execution_date or created.date()).isoformat()}). Ticket bleibt offen bis der Banksync die Zahlung meldet.")
            except Exception:  # noqa: BLE001
                log.exception("note failed")
    audit.log(db, "payment_file.created", tenant_id=tenant.id, actor=actor, entity="payment_file", details={"file": name, "count": len(rows), "total": round(sum(float(r["amount"]) for r in rows), 2)})
    db.commit()
    return name, data, rows


def list_files(tenant: Tenant) -> list[dict]:
    folder = os.path.join(get_settings().data_dir, "payments", tenant.code)
    if not os.path.isdir(folder):
        return []
    out = []
    for n in sorted(os.listdir(folder), reverse=True):
        if n.endswith(".xml"):
            st = os.stat(os.path.join(folder, n))
            out.append({"name": n, "size": st.st_size, "at": datetime.fromtimestamp(st.st_mtime)})
    return out[:50]


def file_path(tenant: Tenant, name: str) -> str | None:
    if not name.endswith(".xml") or "/" in name or ".." in name:
        return None
    path = os.path.join(get_settings().data_dir, "payments", tenant.code, name)
    return path if os.path.isfile(path) else None


def unmark(db: Session, booking: Booking, actor: str) -> None:
    """Remove a booking from its payment file bookkeeping (file was not uploaded), so it can be included again."""
    audit.log(db, "payment_file.unmarked", tenant_id=booking.tenant_id, actor=actor, entity="booking", entity_id=booking.id, details={"file": booking.payment_file})
    booking.payment_file, booking.payment_file_at = None, None
    db.commit()
