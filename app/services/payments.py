"""Payment files: open purchase bills -> pain.001 for upload in e-banking."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Booking, Document, Tenant, now
from . import audit, bexio as bx, pain001
from .zammad import PENDING_REMINDER, ZammadClient

log = logging.getLogger(__name__)


def local_today() -> date:
    from zoneinfo import ZoneInfo

    return now().astimezone(ZoneInfo(get_settings().tz)).date()


def wait_until(today: date | None = None) -> datetime:
    """Ende der Wartezeit: heute + payment_wait_days, payment_wait_hour Uhr Ortszeit (aware)."""
    from zoneinfo import ZoneInfo

    s = get_settings()
    day = (today or local_today()) + timedelta(days=s.payment_wait_days)
    return datetime.combine(day, time(s.payment_wait_hour), tzinfo=ZoneInfo(s.tz))


def _fmt(dt: datetime) -> str:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(get_settings().tz)).strftime("%d.%m.%Y")


def set_waiting(db: Session, z: ZammadClient, booking: Booking, note: str) -> datetime:
    """Notiz ins Ticket, Ticket auf "pending reminder" bis wait_until(), Zeitpunkt an der Buchung merken."""
    until = wait_until()
    tid = booking.document.zammad_ticket_id
    z.add_note(tid, f"{note} Ticket auf Warten bis {_fmt(until)}.")
    z.set_pending(tid, until)
    booking.ticket_wait_until = until
    audit.log(db, "ticket.waiting", tenant_id=booking.tenant_id, entity="document", entity_id=booking.document_id,
              details={"until": until.isoformat(), "file": booking.payment_file})
    return until


def keep_waiting(db: Session, z: ZammadClient, booking: Booking) -> str | None:
    """Rechnung ist im Zahlfile, in bexio aber noch nicht bezahlt. Ticket auf Warten halten:
    - wartet und Wartezeit abgelaufen: erneut + payment_wait_days
    - wartet noch: nichts
    - offen/neu und vom Tool noch nie auf Warten gesetzt (Altfall vor dieser Regel): auf Warten setzen
    - offen/neu, obwohl das Tool es schon auf Warten gesetzt hatte (neue Mail, von Hand geöffnet): nicht anfassen
    - geschlossen oder anderer Status: nicht anfassen
    Gibt "waiting", "rewaiting" oder None zurück."""
    tid = booking.document.zammad_ticket_id if booking.document else None
    if not tid or not booking.payment_file:
        return None
    state, pending = z.ticket_state(tid)
    if state == PENDING_REMINDER:
        if pending is not None and pending > now():
            return None
        set_waiting(db, z, booking, "finance: Rechnung ist in bexio noch nicht als bezahlt abgeglichen "
                                    f"(Zahlfile {booking.payment_file}), erneut geprüft.")
        return "rewaiting"
    if state in ("new", "open") and booking.ticket_wait_until is None:
        set_waiting(db, z, booking, f"finance: Zahlung im Zahlfile {booking.payment_file} enthalten, "
                                    "in bexio noch nicht als bezahlt abgeglichen.")
        return "waiting"
    return None


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
    country = (qr.get("creditor_country") or llm.get("supplier_country") or (sup.country if sup else "") or "").upper()
    if not country and iban:
        country = iban[:2]
    street = (qr.get("creditor_street") or "").strip()
    if street and qr.get("creditor_building"):
        street = f"{street} {qr['creditor_building'].strip()}"
    street = street or (llm.get("supplier_street") or "").strip() or (sup.street if sup else "") or ""
    postcode = (qr.get("creditor_postcode") or llm.get("supplier_postcode") or (sup.postcode if sup else "") or "").strip()
    town = (qr.get("creditor_town") or llm.get("supplier_city") or (sup.city if sup else "") or "").strip()
    problems = []
    if not iban:
        problems.append("keine IBAN")
    elif not pain001.iban_valid(iban):
        problems.append("IBAN ungültig")
    elif pain001.is_qr_iban(iban) and ref_type != "QRR":
        problems.append("QR-IBAN ohne QR-Referenz")
    elif iban[:2] not in ("CH", "LI") and not town:
        problems.append("Adresse (Ort) des Empfängers fehlt")
    if not (dec.get("amount") or 0) > 0:
        problems.append("Betrag fehlt")
    return {"iban": iban, "reference_type": ref_type, "reference": reference, "message": message, "creditor_name": name,
            "country": country, "street": street, "postcode": postcode, "town": town, "problems": problems}


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
                                message=r["message"], country=r["country"], street=r["street"], postcode=r["postcode"], town=r["town"])
                for r in rows]
    from zoneinfo import ZoneInfo

    created = now().astimezone(ZoneInfo(get_settings().tz))
    execution_date = pain001.next_bank_day(execution_date or created.date())
    if execution_date < created.date():
        raise ValueError("Ausführungsdatum liegt in der Vergangenheit")
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
                set_waiting(db, z, b, f"finance: Zahlung im Zahlfile {name} enthalten ({r['currency']} {float(r['amount']):.2f}, "
                                      f"Ausführung {execution_date.strftime('%d.%m.%Y')}). Abschluss, sobald die Rechnung in bexio als bezahlt abgeglichen ist.")
            except Exception:  # noqa: BLE001
                log.exception("zammad wait failed for %s", b.document.zammad_ticket_id)
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


def _bill_paid(db: Session, booking: Booking) -> bool:
    """True, wenn die Rechnung im Tool oder live in bexio bezahlt ist. Ist bexio nicht erreichbar, gilt sie als
    möglicherweise bezahlt (lieber nicht abmelden als doppelt zahlen)."""
    if booking.paid_at or str(booking.bexio_status or "").lower() == "paid":
        return True
    if not booking.bexio_bill_id:
        return False
    tenant = booking.tenant or db.get(Tenant, booking.tenant_id)
    client = bx.BexioClient(db, tenant)
    try:
        status = str(client.get_bill(booking.bexio_bill_id).get("status", "")).lower()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"bexio-Status der Rechnung nicht prüfbar ({e}), Markierung bleibt") from e
    finally:
        client.close()
    booking.bexio_status = status
    return status == "paid"


def unmark(db: Session, z: ZammadClient | None, booking: Booking, actor: str) -> None:
    """Remove a booking from its payment file bookkeeping (file was not uploaded), so it can be included again.
    Refused when the bill is already paid. The ticket gets a note (the old file must not be used) and is opened again."""
    if not booking.payment_file:
        raise ValueError("Buchung ist in keinem Zahlfile")
    if _bill_paid(db, booking):
        db.commit()
        raise ValueError("Rechnung ist in bexio bereits bezahlt, Markierung bleibt")
    old = booking.payment_file
    audit.log(db, "payment_file.unmarked", tenant_id=booking.tenant_id, actor=actor, entity="booking", entity_id=booking.id, details={"file": old})
    booking.payment_file, booking.payment_file_at, booking.ticket_wait_until = None, None, None
    db.commit()
    tid = booking.document.zammad_ticket_id if booking.document else None
    if z and tid:
        try:
            z.add_note(tid, f"finance: Zahlung aus Zahlfile {old} zurückgezogen, dieses Zahlfile nicht verwenden. "
                            "Rechnung ist wieder offen und kommt ins nächste Zahlfile.")
            z.set_state(tid, "open")
        except Exception:  # noqa: BLE001
            log.exception("zammad feedback after unmark failed for %s", tid)
