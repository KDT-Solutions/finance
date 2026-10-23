"""End-to-end processing: Zammad ticket -> PDF -> extraction -> rule -> decision -> bexio -> ticket note."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import KV, Booking, Document, Extraction, PaymentAccount, Rule, Supplier, Tenant, now
from . import audit, bexio as bx, extract, qr as qrmod, rules
from .zammad import ZammadClient

log = logging.getLogger(__name__)


# --- storage --------------------------------------------------------------------------
def store_pdf(tenant: Tenant, sha: str, content: bytes) -> str:
    base = os.path.join(get_settings().data_dir, "docs", tenant.code)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{sha}.pdf")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path


def read_pdf(doc: Document) -> bytes:
    with open(doc.storage_path, "rb") as f:
        return f.read()


def _kv_get(db: Session, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def _kv_set(db: Session, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))


# --- ingest -----------------------------------------------------------------------------
def ingest_tenant(db: Session, z: ZammadClient, tenant: Tenant) -> int:
    """Pull new PDF attachments from the tenant's Finance groups. Returns number of new documents."""
    s = get_settings()
    new = 0
    for group in (tenant.zammad_group_paid, tenant.zammad_group_topay):
        try:
            tickets = z.open_tickets_in_group(group, limit=s.zammad_batch)
        except Exception:  # noqa: BLE001
            log.exception("zammad search failed for %s", group)
            continue
        for t in tickets:
            tid = int(t["id"])
            stamp = f"{t.get('updated_at')}|{t.get('article_count')}"
            key = f"zammad_seen:{tenant.code}:{tid}"
            if _kv_get(db, key) == stamp:
                continue
            try:
                atts = z.pdf_attachments(tid)
                for att in atts:
                    if att.size and att.size > s.max_pdf_bytes:
                        log.warning("attachment too large: ticket %s %s", tid, att.filename)
                        continue
                    content = z.download(att)
                    if not content.startswith(b"%PDF"):
                        continue
                    sha = hashlib.sha256(content).hexdigest()
                    dup = db.query(Document).filter(Document.tenant_id == tenant.id, Document.sha256 == sha).first()
                    if dup:
                        if dup.zammad_ticket_id != tid:
                            z.add_note(tid, f"finance: Dieses PDF wurde bereits in Ticket#{dup.zammad_ticket_number} verarbeitet (Status {dup.state}).")
                        continue
                    path = store_pdf(tenant, sha, content)
                    doc = Document(tenant_id=tenant.id, sha256=sha, filename=att.filename, storage_path=path, size=len(content),
                                   zammad_ticket_id=tid, zammad_ticket_number=str(t.get("number") or ""), zammad_article_id=att.article_id,
                                   zammad_group=group, ticket_title=(t.get("title") or "")[:300])
                    db.add(doc)
                    new += 1
                    audit.log(db, "document.ingested", tenant_id=tenant.id, entity="document", details={"ticket": t.get("number"), "file": att.filename})
                _kv_set(db, key, stamp)
                db.commit()
            except Exception:  # noqa: BLE001
                log.exception("ingest failed for ticket %s", tid)
                db.rollback()
    return new


# --- processing ---------------------------------------------------------------------------
def process_document(db: Session, z: ZammadClient | None, doc: Document) -> None:
    tenant = doc.tenant
    s = get_settings()
    try:
        pdf = read_pdf(doc)
        swiss = qrmod.read_swiss_qr(pdf)
        qr = None
        if swiss:
            qr = swiss.to_dict()
            qr["billing"] = qrmod.parse_billing_info(swiss.billing_info)
        text = qrmod.extract_text(pdf)
        context = ""
        if z and doc.zammad_ticket_id:
            try:
                context = f"Ticket: {doc.ticket_title}\n" + z.article_bodies(doc.zammad_ticket_id)
            except Exception:  # noqa: BLE001
                context = f"Ticket: {doc.ticket_title}"
        llm, model = extract.extract(pdf, context)
        ex = doc.extraction or Extraction(document_id=doc.id)
        ex.qr, ex.llm, ex.model, ex.prompt_version, ex.text_excerpt = qr, llm, model, extract.PROMPT_VERSION, text[:3000]
        db.add(ex)
        doc.extraction = ex

        if not llm.get("is_receipt", True):
            doc.state = "not_a_receipt"
            doc.review_reason = "LLM: kein Beleg (z.B. AGB, Lieferschein, Werbung)"
            audit.log(db, "document.not_a_receipt", tenant_id=tenant.id, entity="document", entity_id=doc.id)
            db.commit()
            return

        # supplier
        name = (qr or {}).get("creditor_name") or llm.get("supplier_name") or ""
        iban = (qr or {}).get("iban") or llm.get("supplier_iban") or ""
        uid = llm.get("supplier_uid") or (qr or {}).get("billing", {}).get("uid", "")
        supplier, how = rules.match_supplier(db, tenant.id, iban=iban, uid=uid, name=name)
        if supplier is None and name:
            supplier = Supplier(tenant_id=tenant.id, display_name=name[:200], name_key=rules.normalize_name(name),
                                uid=rules.normalize_uid(uid) or None, ibans=[rules.normalize_iban(iban)] if iban else [],
                                name_variants=[], is_foreign=bool(llm.get("supplier_country") and llm.get("supplier_country") != "CH"))
            db.add(supplier)
            db.flush()
            how = "new"
        elif supplier is not None:
            changed = False
            if iban and rules.normalize_iban(iban) not in [rules.normalize_iban(x) for x in (supplier.ibans or [])]:
                supplier.ibans = [*(supplier.ibans or []), rules.normalize_iban(iban)]
                changed = True
            if uid and not supplier.uid:
                supplier.uid = rules.normalize_uid(uid) or None
                changed = True
            if name and rules.normalize_name(name) != supplier.name_key and rules.normalize_name(name) not in [rules.normalize_name(v) for v in (supplier.name_variants or [])]:
                supplier.name_variants = [*(supplier.name_variants or []), name[:200]]
                changed = True
            if changed:
                db.add(supplier)
        doc.supplier = supplier
        rule = rules.find_rule(supplier, text + " " + json.dumps(llm.get("line_items") or [], ensure_ascii=False)) if supplier else None
        doc.rule = rule
        decision = rules.decide(db, tenant, group=doc.zammad_group or "", qr=qr, llm=llm, text=text, supplier=supplier, rule=rule, matched_by=how)
        doc.decision = decision.to_dict()
        doc.state = "extracted"
        db.commit()

        # move unpaid invoices from the Finance group into toPay (the tool takes over the sorting)
        if decision.move_to_topay and z and doc.zammad_ticket_id:
            try:
                z.set_group(doc.zammad_ticket_id, tenant.zammad_group_topay)
                doc.zammad_group = tenant.zammad_group_topay
                audit.log(db, "ticket.moved_topay", tenant_id=tenant.id, entity="document", entity_id=doc.id)
            except Exception:  # noqa: BLE001
                log.exception("could not move ticket %s", doc.zammad_ticket_id)

        if decision.auto:
            book(db, z, doc, actor="system")
        else:
            doc.state = "needs_review"
            doc.review_reason = "; ".join(decision.reasons) or "Regel ohne auto_book"
            if z and doc.zammad_ticket_id:
                try:
                    z.add_note(doc.zammad_ticket_id, f"finance: Beleg wartet im Tool zur Prüfung.\nGrund: {doc.review_reason}\n{s.public_base_url.rstrip('/')}/docs/{doc.id}")
                except Exception:  # noqa: BLE001
                    log.exception("note failed")
            audit.log(db, "document.needs_review", tenant_id=tenant.id, entity="document", entity_id=doc.id, details={"reasons": decision.reasons})
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("processing failed for document %s", doc.id)
        db.rollback()
        doc.state = "error"
        doc.last_error = f"{type(e).__name__}: {e}"[:2000]
        db.add(doc)
        audit.log(db, "document.error", tenant_id=doc.tenant_id, entity="document", entity_id=doc.id, details={"error": doc.last_error})
        db.commit()


# --- booking ------------------------------------------------------------------------------
def _master(tenant: Tenant, kind: str, id_: int | None) -> dict:
    for row in (tenant.master_data or {}).get(kind, []):
        if row.get("id") == id_:
            return row
    return {}


def _ensure_contact(client: bx.BexioClient, supplier: Supplier, llm: dict) -> int:
    if supplier.bexio_contact_id:
        return supplier.bexio_contact_id
    hits = client.search_contacts(supplier.display_name[:40])
    key = rules.normalize_name(supplier.display_name)
    for c in hits:
        if rules.normalize_name(c.get("name_1", "")) == key:
            supplier.bexio_contact_id = c["id"]
            return c["id"]
    c = client.create_contact(supplier.display_name)
    supplier.bexio_contact_id = c["id"]
    return c["id"]


def book(db: Session, z: ZammadClient | None, doc: Document, *, actor: str = "system") -> Booking:
    """Book the document according to doc.decision. Raises on bexio errors (state -> error)."""
    tenant = doc.tenant
    dec = dict(doc.decision or {})
    client = bx.BexioClient(db, tenant)
    try:
        if doc.booking and doc.booking.status == "ok":
            return doc.booking
        if not doc.supplier:
            raise RuntimeError("Lieferant fehlt")
        pdf = read_pdf(doc)
        llm = (doc.extraction.llm if doc.extraction else {}) or {}
        qr = (doc.extraction.qr if doc.extraction else None) or None
        contact_id = _ensure_contact(client, doc.supplier, llm)
        amount = float(dec.get("amount") or 0)
        if amount <= 0:
            raise RuntimeError("Betrag fehlt oder 0")
        mode = dec.get("payment_mode") or "open"
        desc = (dec.get("booking_text") or dec.get("supplier_name") or "")[:200]
        docno = dec.get("document_number") or ""
        booking = Booking(document_id=doc.id, tenant_id=tenant.id, amount=amount, currency=dec.get("currency") or "CHF")

        if mode == "open":
            existing = client.find_bill(contact_id, docno)
            if existing:
                booking.kind, booking.bexio_bill_id, booking.response, booking.bexio_status = "bill_open", str(existing.get("id")), existing, existing.get("status")
            else:
                f = client.upload_file(doc.filename or "beleg.pdf", pdf)
                file_id = str(f.get("uuid") or f.get("id"))
                payload = bx.build_bill_payload(
                    supplier_id=contact_id, contact_partner_id=None, document_no=docno, title=desc or docno,
                    bill_date=dec.get("doc_date") or now().date().isoformat(), due_date=dec.get("due_date") or "",
                    currency=dec.get("currency") or "CHF", amount_gross=amount, account_id=int(dec["account_id"]),
                    tax_id=dec.get("tax_id"), text=desc, attachment_ids=[file_id], qr=qr,
                )
                created = client.create_bill(payload)
                bill_id = str(created.get("id"))
                try:
                    client.set_bill_status(bill_id, "booked")
                    status = "booked"
                except bx.BexioError as e:
                    log.warning("bill %s created but not booked: %s", bill_id, e)
                    status = "draft"
                booking.kind, booking.bexio_bill_id, booking.bexio_file_id, booking.payload, booking.response, booking.bexio_status = "bill_open", bill_id, file_id, payload, created, status
            note = f"finance: Kreditorenrechnung in bexio angelegt (offen).\nLieferant: {dec.get('supplier_name')}\nBetrag: {booking.currency} {amount:.2f}\nKonto: {_master(tenant, 'accounts', dec.get('account_id')).get('no', dec.get('account_id'))}, Steuercode: {_master(tenant, 'taxes', dec.get('tax_id')).get('code', dec.get('tax_id') or '-')}\nbexio Bill-ID: {booking.bexio_bill_id}"
            close_ticket = False
        else:
            pa = db.get(PaymentAccount, dec.get("payment_account_id")) if dec.get("payment_account_id") else None
            if not pa or not pa.bexio_account_id:
                raise RuntimeError("Zahlkonto ohne bexio-Konto")
            tax = _master(tenant, "taxes", dec.get("tax_id"))
            lines = bx.build_manual_entry_lines(amount=amount, debit_account_id=int(dec["account_id"]), credit_account_id=pa.bexio_account_id,
                                                tax_id=dec.get("tax_id"), tax_account_id=int(dec["account_id"]), description=f"{desc} {docno}".strip())
            payload = {"date": dec.get("doc_date") or now().date().isoformat(), "reference_nr": docno or doc.filename, "entries": lines}
            created = client.create_manual_entry(payload["date"], payload["reference_nr"], lines)
            entry_id = created.get("id")
            line_id = (created.get("entries") or [{}])[0].get("id")
            file_info = None
            if entry_id and line_id:
                try:
                    file_info = client.attach_file_to_entry(entry_id, line_id, doc.filename or "beleg.pdf", pdf)
                except bx.BexioError as e:
                    log.warning("file attach failed: %s", e)
            booking.kind, booking.bexio_entry_id, booking.payload, booking.response = "entry", entry_id, payload, {"entry": created, "file": file_info}
            booking.bexio_file_id = str((file_info or {}).get("uuid") or (file_info or {}).get("id") or "") or None
            note = f"finance: Beleg gebucht (bezahlt).\nLieferant: {dec.get('supplier_name')}\nBetrag: {booking.currency} {amount:.2f}\nKonto: {_master(tenant, 'accounts', dec.get('account_id')).get('no', dec.get('account_id'))} an {pa.label}, Steuercode: {tax.get('code', dec.get('tax_id') or '-')}\nbexio Buchung-ID: {entry_id}"
            close_ticket = True

        db.add(booking)
        doc.booking = booking
        doc.state = "booked"
        doc.last_error = None
        db.add(doc)
        audit.log(db, "document.booked", tenant_id=tenant.id, actor=actor, entity="document", entity_id=doc.id,
                  details={"kind": booking.kind, "bexio_id": booking.bexio_bill_id or booking.bexio_entry_id, "amount": amount})
        db.commit()
        if z and doc.zammad_ticket_id:
            try:
                z.add_note(doc.zammad_ticket_id, note)
                if close_ticket:
                    z.set_state(doc.zammad_ticket_id, "closed")
                    audit.log(db, "ticket.closed", tenant_id=tenant.id, entity="document", entity_id=doc.id)
                    db.commit()
            except Exception:  # noqa: BLE001
                log.exception("zammad feedback failed for %s", doc.zammad_ticket_id)
        return booking
    except Exception as e:  # noqa: BLE001
        db.rollback()
        doc.state = "error"
        doc.last_error = f"{type(e).__name__}: {e}"[:2000]
        db.add(doc)
        audit.log(db, "document.booking_error", tenant_id=tenant.id, actor=actor, entity="document", entity_id=doc.id, details={"error": doc.last_error})
        db.commit()
        raise
    finally:
        client.close()


# --- open bills: close ticket once paid ---------------------------------------------------
def check_paid(db: Session, z: ZammadClient, tenant: Tenant) -> int:
    client = bx.BexioClient(db, tenant)
    closed = 0
    try:
        q = db.query(Booking).join(Document).filter(Booking.tenant_id == tenant.id, Booking.kind == "bill_open", Booking.paid_at.is_(None), Booking.status == "ok")
        for b in q.all():
            try:
                bill = client.get_bill(b.bexio_bill_id)
            except bx.BexioError as e:
                log.warning("get_bill %s failed: %s", b.bexio_bill_id, e)
                continue
            status = str(bill.get("status", "")).lower()
            b.bexio_status = status
            if status == "paid":
                b.paid_at = now()
                db.add(b)
                if b.document.zammad_ticket_id:
                    try:
                        z.add_note(b.document.zammad_ticket_id, "finance: Rechnung ist in bexio als bezahlt abgeglichen, Ticket geschlossen.")
                        z.set_state(b.document.zammad_ticket_id, "closed")
                        closed += 1
                    except Exception:  # noqa: BLE001
                        log.exception("close failed")
                audit.log(db, "bill.paid", tenant_id=tenant.id, entity="document", entity_id=b.document_id)
            db.commit()
    finally:
        client.close()
    return closed


# --- history import ---------------------------------------------------------------------------
def learn_from_history(db: Session, tenant: Tenant, months: int = 24) -> int:
    """Seed supplier rules from existing purchase bills in bexio (account, tax, amount band)."""
    client = bx.BexioClient(db, tenant)
    created = 0
    try:
        stats: dict[int, dict] = {}
        offset = 0
        while True:
            res = client.list_bills(limit=500, offset=offset, status="paid")
            rows = res.get("data", []) if isinstance(res, dict) else res
            if not rows:
                break
            for b in rows:
                sid = b.get("supplier_id")
                if not sid:
                    continue
                st = stats.setdefault(sid, {"name": b.get("vendor") or b.get("lastname_company") or "", "accounts": {}, "amounts": []})
                for acc in b.get("booking_account_ids") or []:
                    st["accounts"][acc] = st["accounts"].get(acc, 0) + 1
                if b.get("gross"):
                    st["amounts"].append(float(b["gross"]))
            if len(rows) < 500:
                break
            offset += 500
        for sid, st in stats.items():
            if not st["accounts"] or not st["name"]:
                continue
            acc, n = max(st["accounts"].items(), key=lambda kv: kv[1])
            sup = db.query(Supplier).filter(Supplier.tenant_id == tenant.id, Supplier.bexio_contact_id == sid).first()
            if not sup:
                sup = db.query(Supplier).filter(Supplier.tenant_id == tenant.id, Supplier.name_key == rules.normalize_name(st["name"])).first()
            if not sup:
                sup = Supplier(tenant_id=tenant.id, bexio_contact_id=sid, display_name=st["name"][:200], name_key=rules.normalize_name(st["name"]), ibans=[], name_variants=[])
                db.add(sup)
                db.flush()
            else:
                sup.bexio_contact_id = sup.bexio_contact_id or sid
            if not any(not (r.keywords or []) for r in sup.rules):
                rule = Rule(tenant_id=tenant.id, supplier_id=sup.id, keywords=[], account_id=acc, tax_id=tenant.default_tax_id,
                            payment_mode="open", auto_book=n >= 2 and len(st["accounts"]) == 1, learned_from="history", hits=n,
                            amount_min=min(st["amounts"]) * 0.5 if st["amounts"] else None, amount_max=max(st["amounts"]) * 2 if st["amounts"] else None)
                db.add(rule)
                created += 1
        db.commit()
    finally:
        client.close()
    return created


def summary_text(db: Session) -> str:
    from sqlalchemy import func

    rows = db.query(Tenant.code, func.count(Document.id)).join(Document).filter(Document.state == "needs_review").group_by(Tenant.code).all()
    if not rows:
        return ""
    return ", ".join(f"{code}: {n}" for code, n in rows)
