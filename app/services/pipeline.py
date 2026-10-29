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
from . import audit, bexio as bx, extract, fx, payments, qr as qrmod, rules
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
def invoices_over_receipts(atts: list) -> list:
    """Stripe mails carry Invoice-*.pdf and Receipt-*.pdf for the same payment: within one article,
    drop the receipts when an invoice is attached. A receipt on its own is kept."""
    def kind(a) -> str:
        name = (a.filename or "").lower()
        return "invoice" if name.startswith("invoice") else "receipt" if name.startswith("receipt") else ""

    with_invoice = {a.article_id for a in atts if kind(a) == "invoice"}
    return [a for a in atts if not (kind(a) == "receipt" and a.article_id in with_invoice)]


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
                atts = invoices_over_receipts(z.pdf_attachments(tid))
                # the same PDF often hangs at the mail and at its forward in one ticket; the first copy is not in the
                # database yet (no autoflush), so remember the hashes of this ticket as well
                seen: set[str] = set()
                for att in atts:
                    if att.size and att.size > s.max_pdf_bytes:
                        log.warning("attachment too large: ticket %s %s", tid, att.filename)
                        continue
                    content = z.download(att)
                    if not content.startswith(b"%PDF"):
                        continue
                    sha = hashlib.sha256(content).hexdigest()
                    if sha in seen:
                        continue
                    seen.add(sha)
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
    doc.last_error = None
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
        ai = None
        if supplier is None and name:
            # IBAN, UID and name found nothing: let the model pick among the most similar known suppliers
            # (brand vs. legal name, e.g. "Backbone Solutions AG (sipcall.ch)" = "sipcall by Backbone Solutions AG")
            candidates = rules.supplier_candidates(db, tenant.id, name)
            choice = extract.choose_supplier({"name": name, "uid": uid, "iban": iban, "street": llm.get("supplier_street"),
                                              "postcode": llm.get("supplier_postcode"), "city": llm.get("supplier_city"),
                                              "country": llm.get("supplier_country"), "email_subject": doc.ticket_title}, candidates)
            ids = {c["id"] for c in candidates}
            if choice and choice.get("supplier_id") in ids and float(choice.get("confidence") or 0) >= 0.7:
                supplier, how = db.get(Supplier, choice["supplier_id"]), "ai"
                ai = {"supplier_id": supplier.id, "name": name, "uid": uid, "iban": iban, "reason": (choice.get("reason") or "")[:300]}
        if supplier is None and name:
            supplier = Supplier(tenant_id=tenant.id, display_name=name[:200], name_key=rules.normalize_name(name),
                                uid=rules.normalize_uid(uid) or None, ibans=[rules.normalize_iban(iban)] if iban else [],
                                name_variants=[], is_foreign=bool(llm.get("supplier_country") and llm.get("supplier_country") != "CH"))
            _fill_address(supplier, qr, llm)
            db.add(supplier)
            db.flush()
            how = "new"
        elif supplier is not None and how != "ai":  # an AI match learns the spelling only once a person confirms it
            changed = False
            if iban and rules.normalize_iban(iban) not in [rules.normalize_iban(x) for x in (supplier.ibans or [])]:
                supplier.ibans = [*(supplier.ibans or []), rules.normalize_iban(iban)]
                changed = True
            if uid and not supplier.uid:
                supplier.uid = rules.normalize_uid(uid) or None
                changed = True
            if _fill_address(supplier, qr, llm):
                changed = True
            if name and rules.normalize_name(name) != supplier.name_key and rules.normalize_name(name) not in [rules.normalize_name(v) for v in (supplier.name_variants or [])]:
                supplier.name_variants = [*(supplier.name_variants or []), name[:200]]
                changed = True
            if changed:
                db.add(supplier)
        doc.supplier = supplier
        rule = rules.find_rule(supplier, text + " " + json.dumps(llm.get("line_items") or [], ensure_ascii=False),
                               credit=rules.is_commission(llm)) if supplier else None
        doc.rule = rule
        decision = rules.decide(db, tenant, group=doc.zammad_group or "", qr=qr, llm=llm, text=text, supplier=supplier, rule=rule, matched_by=how)
        if ai:
            decision.reasons.append(f"Lieferant per KI zugeordnet: {supplier.display_name} ({ai['reason']}), prüfen")
            decision.auto = False
            # the decision carries the chosen supplier's name, else booking would rename it to the spelling on the document
            decision.supplier_name = supplier.display_name
        doc.decision = decision.to_dict()
        if ai:
            doc.decision = {**doc.decision, "ai_supplier": ai}
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

        if decision.auto and decision.needs_chf:
            doc.state = "needs_chf"
            doc.review_reason = f"CHF-Betrag fehlt ({decision.currency} {decision.amount:.2f} bezahlt)"
            if z and doc.zammad_ticket_id:
                try:
                    z.add_note(doc.zammad_ticket_id, f"finance: Beleg in {decision.currency}, wartet auf den CHF-Betrag laut Abrechnung.\n{s.public_base_url.rstrip('/')}/chf")
                except Exception:  # noqa: BLE001
                    log.exception("note failed")
            audit.log(db, "document.needs_chf", tenant_id=tenant.id, entity="document", entity_id=doc.id,
                      details={"currency": decision.currency, "amount": decision.amount})
        elif decision.auto:
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
def _one(res) -> dict:
    """bexio answers some POSTs with an object, others with a list of objects: first object or {}."""
    if isinstance(res, list):
        res = res[0] if res else None
    return res if isinstance(res, dict) else {}


def attach_credit_pdf(client: bx.BexioClient, doc: Document, invoice_id, pdf: bytes) -> str | None:
    """Commission credit note: upload the PDF to bexio files and point to it with an internal comment on the debtor
    invoice (the API cannot attach files to debtor invoices). Never fails the booking, returns the file id or None."""
    try:
        f = _one(client.upload_file(doc.filename or "gutschrift.pdf", pdf))
        file_id = str(f.get("uuid") or f.get("id") or "") or None
        me = client.me()
        user = " ".join(x for x in (me.get("firstname"), me.get("lastname")) if x) or me.get("email") or "finance"
        link = f"{get_settings().public_base_url.rstrip('/')}/docs/{doc.id}/pdf"
        client.add_invoice_comment(int(invoice_id), f"Beleg: {link} ({doc.filename}, auch in bexio Dateien, ID {file_id}), "
                                                    f"Ticket#{doc.zammad_ticket_number or '-'}",
                                   me.get("id") or doc.tenant.bexio_user_id, user)
        return file_id
    except Exception as e:  # noqa: BLE001
        log.warning("credit note pdf for invoice %s not attached: %s", invoice_id, e)
        return None


def _positions(dec: dict, amount: float) -> list[dict] | None:
    """Positions of the decision (several VAT rates); None for a single line. They must add up to the amount."""
    lines = [ln for ln in (dec.get("lines") or []) if ln.get("amount")]
    if len(lines) < 2:
        return None
    total = round(sum(float(ln["amount"]) for ln in lines), 2)
    if abs(total - amount) > 0.005:
        raise RuntimeError(f"Positionen ergeben {total:.2f}, Betrag ist {amount:.2f}")
    return lines


def _positions_note(tenant: Tenant, lines: list[dict] | None) -> str:
    if not lines:
        return ""
    parts = [f"{float(ln['amount']):.2f} {_master(tenant, 'taxes', ln.get('tax_id')).get('code', 'ohne Steuer')}" for ln in lines]
    return "\nPositionen: " + ", ".join(parts)


def _master(tenant: Tenant, kind: str, id_: int | None) -> dict:
    for row in (tenant.master_data or {}).get(kind, []):
        if row.get("id") == id_:
            return row
    return {}


def _create_bill(db: Session, client: bx.BexioClient, tenant: Tenant, kw: dict) -> tuple[dict, dict]:
    """POST the bill. The spec marks contact_partner_id as required but does not say which id it expects
    (own bexio user or supplier contact). Try the variants in order, remember the one bexio accepts per tenant."""
    key = f"bexio_bill_partner:{tenant.code}"
    variants = [("user", tenant.bexio_user_id), ("supplier", kw["supplier_id"]), ("none", None)]
    known = _kv_get(db, key)
    if known:
        variants.sort(key=lambda v: 0 if v[0] == known else 1)
    last: bx.BexioError | None = None
    for name, pid in variants:
        if name != "none" and not pid:
            continue
        payload = bx.build_bill_payload(contact_partner_id=pid, **kw)
        try:
            created = client.create_bill(payload)
        except bx.BexioError as e:
            msg = str(e)
            if "contact_partner" not in msg and "400" not in msg:
                raise
            log.warning("create_bill variant %s rejected: %s", name, msg[:300])
            last = e
            continue
        if known != name:
            _kv_set(db, key, name)
        return payload, created
    raise last or RuntimeError("create_bill failed")


def _fill_address(supplier: Supplier, qr: dict | None, llm: dict) -> bool:
    """Set the supplier's postal address from the QR part (preferred) or the extraction, only where still empty."""
    qr = qr or {}
    street = (qr.get("creditor_street") or "").strip()
    if street and qr.get("creditor_building"):
        street = f"{street} {qr['creditor_building'].strip()}"
    cand = {
        "street": street or (llm.get("supplier_street") or "").strip(),
        "postcode": (qr.get("creditor_postcode") or llm.get("supplier_postcode") or "").strip(),
        "city": (qr.get("creditor_town") or llm.get("supplier_city") or "").strip(),
        "country": (qr.get("creditor_country") or llm.get("supplier_country") or "").strip().upper()[:2],
    }
    changed = False
    for k, v in cand.items():
        if v and not getattr(supplier, k):
            setattr(supplier, k, v[:100] if k == "street" else v[:60])
            changed = True
    return changed


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
        lines = _positions(dec, amount)
        desc = (dec.get("booking_text") or dec.get("supplier_name") or "")[:200]
        docno = dec.get("document_number") or ""
        booking = Booking(document_id=doc.id, tenant_id=tenant.id, amount=amount, currency=dec.get("currency") or "CHF")

        if mode == "credit":
            if booking.currency.upper() != "CHF":
                raise RuntimeError(f"Provisions-Gutschrift in {booking.currency} wird nicht unterstützt")
            title = f"Gutschrift {docno}".strip() if docno else (desc or "Gutschrift")
            payload = bx.build_invoice_payload(
                contact_id=contact_id, user_id=tenant.bexio_user_id or client.me().get("id"), title=title,
                doc_date=dec.get("doc_date") or now().date().isoformat(), due_date=dec.get("due_date") or "",
                amount_gross=amount, account_id=int(dec["account_id"]), tax_id=dec.get("tax_id"),
                text=f"{desc} {docno}".strip(), reference=docno)
            created = _one(client.create_invoice(payload))
            invoice_id = created.get("id")
            status = "draft"
            try:
                client.issue_invoice(invoice_id)
                status = "pending"
            except bx.BexioError as e:
                log.warning("invoice %s created but not issued: %s", invoice_id, e)
            booking.kind, booking.bexio_bill_id, booking.payload, booking.response, booking.bexio_status = "credit_invoice", str(invoice_id), payload, created, status
            booking.bexio_file_id = attach_credit_pdf(client, doc, invoice_id, pdf)
            note = (f"finance: Provisions-Gutschrift als Debitorenrechnung in bexio angelegt ({status}), Eingang über den Banksync.\n"
                    f"Partner: {dec.get('supplier_name')}\nBetrag: {booking.currency} {amount:.2f}\n"
                    f"Konto: {_master(tenant, 'accounts', dec.get('account_id')).get('no', dec.get('account_id'))}, "
                    f"Steuercode: {_master(tenant, 'taxes', dec.get('tax_id')).get('code', dec.get('tax_id') or '-')}\n"
                    f"bexio: {bx.invoice_web_url(invoice_id)} {created.get('document_nr') or ''}".rstrip())
            close_ticket = True
        elif mode == "open":
            existing = client.find_bill(contact_id, docno)
            if existing:
                booking.kind, booking.bexio_bill_id, booking.response, booking.bexio_status = "bill_open", str(existing.get("id")), existing, existing.get("status")
            else:
                currency = (dec.get("currency") or "CHF").upper()
                rate = dec.get("exchange_rate") or (1.0 if currency == "CHF" else (fx.bazg_rate(currency, dec.get("doc_date") or "") or client.exchange_rate(currency, dec.get("doc_date") or "")))
                if currency != "CHF" and not rate:
                    raise RuntimeError(f"Kein Wechselkurs für {currency} in bexio (Währung in bexio anlegen oder Kurs im Beleg setzen)")
                f = client.upload_file(doc.filename or "beleg.pdf", pdf)
                file_id = str(f.get("uuid") or f.get("id"))
                payload, created = _create_bill(db, client, tenant, dict(
                    supplier_id=contact_id, supplier_name=doc.supplier.display_name if doc.supplier else (dec.get("supplier_name") or ""),
                    document_no=docno, title=desc or docno,
                    bill_date=dec.get("doc_date") or now().date().isoformat(), due_date=dec.get("due_date") or "",
                    currency=dec.get("currency") or "CHF", amount_gross=amount, account_id=int(dec["account_id"]),
                    tax_id=dec.get("tax_id"), text=desc, attachment_ids=[file_id], qr=qr, bank_account_id=tenant.bank_account_id,
                    exchange_rate=rate, supplier_iban=(llm.get("supplier_iban") or ""), lines=lines,
                ))
                bill_id = str(created.get("id"))
                try:
                    client.set_bill_status(bill_id, "BOOKED")
                    status = "BOOKED"
                except bx.BexioError as e:
                    log.warning("bill %s created but not booked: %s", bill_id, e)
                    status = "DRAFT"
                booking.kind, booking.bexio_bill_id, booking.bexio_file_id, booking.payload, booking.response, booking.bexio_status = "bill_open", bill_id, file_id, payload, created, status
            note = f"finance: Kreditorenrechnung in bexio angelegt (offen).\nLieferant: {dec.get('supplier_name')}\nBetrag: {booking.currency} {amount:.2f}\nKonto: {_master(tenant, 'accounts', dec.get('account_id')).get('no', dec.get('account_id'))}, Steuercode: {_master(tenant, 'taxes', dec.get('tax_id')).get('code', dec.get('tax_id') or '-')}\nbexio: {bx.bill_web_url(booking.bexio_bill_id)}"
            # already paid (checkbox / MCP flag, or an agent wrote "bezahlt" into the ticket): close now,
            # the bank sync settles the bill in bexio
            already_paid = bool(dec.get("already_paid"))
            if not already_paid and z and doc.zammad_ticket_id:
                try:
                    already_paid = z.agent_marked_paid(doc.zammad_ticket_id)
                except Exception:  # noqa: BLE001
                    log.exception("paid marker check failed")
            close_ticket = already_paid
            if already_paid:
                note += "\nBereits bezahlt, Ausgleich über den Banksync. Ticket geschlossen."
                booking.paid_at = now()
            note += _positions_note(tenant, lines)
        else:
            pa = db.get(PaymentAccount, dec.get("payment_account_id")) if dec.get("payment_account_id") else None
            if not pa or not pa.bexio_account_id:
                raise RuntimeError("Zahlkonto ohne bexio-Konto")
            tax = _master(tenant, "taxes", dec.get("tax_id"))
            # paid in a foreign currency: the payment account was debited in CHF, book that amount
            original = ""
            if lines and any(ln.get("net") is not None for ln in lines):
                raise RuntimeError("Verzollungsrechnung als bezahlter Beleg wird nicht unterstützt, als offene Rechnung buchen")
            if booking.currency.upper() != "CHF" and lines:
                raise RuntimeError("Positionen bei bezahltem Beleg in Fremdwährung werden nicht unterstützt")
            if booking.currency.upper() != "CHF":
                amount_chf = float(dec.get("amount_chf") or 0)
                if amount_chf <= 0:
                    raise RuntimeError(f"CHF-Betrag fehlt (bezahlt in {booking.currency})")
                original = f"{booking.currency.upper()} {amount:.2f}"
                booking.amount, booking.currency, amount = amount_chf, "CHF", amount_chf
            text = " ".join(x for x in (desc, docno, f"({original})" if original else "") if x)
            if lines:
                entries = bx.build_manual_entry_split(lines=lines, debit_account_id=int(dec["account_id"]), credit_account_id=pa.bexio_account_id, description=text)
            else:
                entries = bx.build_manual_entry_lines(amount=amount, debit_account_id=int(dec["account_id"]), credit_account_id=pa.bexio_account_id,
                                                      tax_id=dec.get("tax_id"), tax_account_id=int(dec["account_id"]), description=text)
            payload = {"date": dec.get("doc_date") or now().date().isoformat(), "reference_nr": docno or doc.filename, "entries": entries}
            if original:
                payload["original"] = original
            created = client.create_manual_entry(payload["date"], payload["reference_nr"], entries)
            # from here on the entry exists in bexio: nothing below may fail the booking, or it is lost locally
            entry = _one(created)
            entry_id = entry.get("id")
            line_id = _one(entry.get("entries")).get("id")
            file_info = None
            if entry_id and line_id:
                try:
                    # bexio answers the file upload with a list of file objects
                    file_info = client.attach_file_to_entry(entry_id, line_id, doc.filename or "beleg.pdf", pdf)
                except Exception as e:  # noqa: BLE001
                    log.warning("file attach failed for entry %s: %s", entry_id, e)
            booking.kind, booking.bexio_entry_id, booking.payload, booking.response = "entry", entry_id, payload, {"entry": created, "file": file_info}
            f = _one(file_info)
            booking.bexio_file_id = str(f.get("uuid") or f.get("id") or "") or None
            note = f"finance: Beleg gebucht (bezahlt).\nLieferant: {dec.get('supplier_name')}\nBetrag: {booking.currency} {amount:.2f}{f' ({original})' if original else ''}\nKonto: {_master(tenant, 'accounts', dec.get('account_id')).get('no', dec.get('account_id'))} an {pa.label}, Steuercode: {tax.get('code', dec.get('tax_id') or '-')}\nbexio Buchung-ID: {entry_id}"
            note += _positions_note(tenant, lines)
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
                b.ticket_wait_until = None
                db.add(b)
                if b.document.zammad_ticket_id:
                    try:
                        # closes open and waiting (pending reminder) tickets alike
                        z.add_note(b.document.zammad_ticket_id, "finance: Rechnung ist in bexio als bezahlt abgeglichen, Ticket geschlossen.")
                        z.set_state(b.document.zammad_ticket_id, "closed")
                        closed += 1
                    except Exception:  # noqa: BLE001
                        log.exception("close failed")
                audit.log(db, "bill.paid", tenant_id=tenant.id, entity="document", entity_id=b.document_id)
            elif b.payment_file:
                # paid via payment file, bank sync not through yet: keep the ticket waiting
                try:
                    payments.keep_waiting(db, z, b)
                except Exception:  # noqa: BLE001
                    log.exception("keep waiting failed for booking %s", b.id)
            db.commit()
    finally:
        client.close()
    return closed


# --- history import ---------------------------------------------------------------------------
def learn_from_history(db: Session, tenant: Tenant, months: int = 24) -> dict:
    """Seed supplier rules from paid purchase bills in bexio: expense account (majority), amount band, and the
    payment source (bank account of the outgoing payment -> payment account -> card/private/bank_paid, else open).
    Returns counts plus the bexio bank accounts that paid bills but are not mapped to a payment account."""
    client = bx.BexioClient(db, tenant)
    created = updated = 0
    unmapped: dict[str, int] = {}
    try:
        # The list response carries no supplier_id (spec: id, document_no, status, vendor, lastname_company, gross,
        # booking_account_ids ...), so bills are grouped by vendor name and the supplier contact id is read once per
        # vendor from the bill detail.
        by_vendor: dict[str, dict] = {}
        page = 1
        while True:
            # status filter values per spec: DRAFTS | TODO | PAID | OVERDUE; paging is 1-based "page"
            try:
                res = client.list_bills(limit=500, page=page, status="PAID", sort="bill_date", order="desc")
            except bx.BexioError:
                res = client.list_bills(limit=500, page=page, status="PAID")
            rows = res.get("data", []) if isinstance(res, dict) else res
            if not rows:
                break
            for b in rows:
                name = (b.get("vendor") or b.get("lastname_company") or "").strip()
                if not name:
                    continue
                st = by_vendor.setdefault(rules.normalize_name(name), {"name": name, "accounts": {}, "amounts": [], "bill_ids": [], "sid": b.get("supplier_id")})
                if len(st["bill_ids"]) < 3 and b.get("id"):
                    st["bill_ids"].append(str(b["id"]))
                for acc in b.get("booking_account_ids") or []:
                    st["accounts"][acc] = st["accounts"].get(acc, 0) + 1
                if b.get("gross"):
                    st["amounts"].append(float(b["gross"]))
            paging = res.get("paging") if isinstance(res, dict) else None
            if paging and page >= int(paging.get("page_count") or 1):
                break
            if len(rows) < 500 or page > 40:
                break
            page += 1
        log.info("history %s: %d paid bills grouped into %d vendors", tenant.code, sum(len(v["amounts"]) for v in by_vendor.values()), len(by_vendor))

        bank_by_id = {b["id"]: b for b in (tenant.master_data or {}).get("bank_accounts", [])}
        pay_accounts = db.query(PaymentAccount).filter(PaymentAccount.tenant_id == tenant.id, PaymentAccount.active.is_(True)).all()
        pa_by_ledger = {pa.bexio_account_id: pa for pa in pay_accounts if pa.bexio_account_id}
        # bills paid by bank transfer are the normal "open bill + bank sync" flow, so a bank payment account
        # keeps payment_mode open; only card and private payments become paid receipts
        mode_for_kind = {"card": "card", "private": "private"}

        for key, st in by_vendor.items():
            sid = st.get("sid")
            if not sid and st["bill_ids"]:
                try:
                    sid = client.get_bill(st["bill_ids"][0]).get("supplier_id")
                except bx.BexioError as e:
                    log.warning("history: bill %s detail failed: %s", st["bill_ids"][0], e)
            if not sid or not st["accounts"] or not st["name"]:
                continue
            # payment source: outgoing payments of the sampled bills
            sources: dict[int, int] = {}
            ibans: set[str] = set()
            for bid in st["bill_ids"]:
                try:
                    pays = client.get("/4.0/purchase/outgoing-payments", bill_id=bid, limit=10) or {}
                except bx.BexioError as e:
                    log.warning("history: payments of bill %s failed: %s", bid, e)
                    continue
                for p in pays.get("data", []) if isinstance(pays, dict) else pays:
                    if p.get("sender_bank_account_id"):
                        sources[p["sender_bank_account_id"]] = sources.get(p["sender_bank_account_id"], 0) + 1
                    if p.get("receiver_iban"):
                        ibans.add(rules.normalize_iban(p["receiver_iban"]))
            payment_mode, payment_account_id = "open", None
            if sources:
                bank_id, _n = max(sources.items(), key=lambda kv: kv[1])
                bank = bank_by_id.get(bank_id, {})
                pa = pa_by_ledger.get(bank.get("account_id"))
                if pa and pa.kind in mode_for_kind:
                    payment_mode, payment_account_id = mode_for_kind[pa.kind], pa.id
                elif pa:
                    payment_mode, payment_account_id = "open", None
                else:
                    label = bank.get("name") or f"bank_account {bank_id}"
                    unmapped[label] = unmapped.get(label, 0) + 1

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
            if ibans:
                sup.ibans = sorted(set(sup.ibans or []) | {i for i in ibans if i})
            default_rule = next((r for r in sup.rules if not (r.keywords or []) and r.payment_mode != "credit"), None)
            if default_rule is None:
                db.add(Rule(tenant_id=tenant.id, supplier_id=sup.id, keywords=[], account_id=acc, tax_id=None,
                            payment_mode=payment_mode, payment_account_id=payment_account_id,
                            auto_book=n >= 2 and len(st["accounts"]) == 1, learned_from="history", hits=n,
                            amount_min=min(st["amounts"]) * 0.5 if st["amounts"] else None, amount_max=max(st["amounts"]) * 2 if st["amounts"] else None))
                created += 1
            elif default_rule.learned_from == "history" and (default_rule.payment_mode, default_rule.payment_account_id) != (payment_mode, payment_account_id):
                # rules confirmed or edited by a person are left alone; history rules follow the latest import
                default_rule.payment_mode, default_rule.payment_account_id = payment_mode, payment_account_id
                updated += 1
        db.commit()
    finally:
        client.close()
    return {"created": created, "updated": updated, "unmapped_bank_accounts": unmapped}


def summary_text(db: Session) -> str:
    from sqlalchemy import func

    rows = db.query(Tenant.code, func.count(Document.id)).join(Document).filter(Document.state.in_(["needs_review", "needs_chf"])).group_by(Tenant.code).all()
    if not rows:
        return ""
    return ", ".join(f"{code}: {n}" for code, n in rows)
