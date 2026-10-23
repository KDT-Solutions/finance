"""User actions shared by the web UI and the MCP endpoint: book with a (possibly edited) decision, skip."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import Document, Supplier
from . import audit, pipeline, rules
from .zammad import ZammadClient

log = logging.getLogger(__name__)


def apply_decision(db: Session, doc: Document, dec: dict) -> dict:
    """Store the decision on the document and resolve/rename the supplier from dec['supplier_name']."""
    name = (dec.get("supplier_name") or "").strip()
    if name and (not doc.supplier or rules.normalize_name(name) != doc.supplier.name_key):
        existing = db.query(Supplier).filter(Supplier.tenant_id == doc.tenant_id, Supplier.name_key == rules.normalize_name(name)).first()
        if existing:
            doc.supplier = existing
        elif doc.supplier and not doc.supplier.rules and db.query(Document).filter(Document.supplier_id == doc.supplier.id).count() <= 1:
            doc.supplier.display_name = name[:200]
            doc.supplier.name_key = rules.normalize_name(name)
        else:
            sup = Supplier(tenant_id=doc.tenant_id, display_name=name[:200], name_key=rules.normalize_name(name), ibans=[], name_variants=[])
            db.add(sup)
            db.flush()
            doc.supplier = sup
    doc.decision = dec
    return dec


def book_with_decision(db: Session, z: ZammadClient | None, doc: Document, dec: dict, *, remember: bool, keywords: list[str], actor: str) -> dict:
    """Apply the decision, move the ticket to toPay for open invoices, book in bexio, optionally learn a rule.
    Returns {"booked": bool, "rule_id": int | None}. Errors are stored on the document (state error / last_error)."""
    apply_decision(db, doc, dec)
    audit.log(db, "document.decided", tenant_id=doc.tenant_id, actor=actor, entity="document", entity_id=doc.id, details=dec)
    out = {"booked": False, "rule_id": None}
    if dec.get("payment_mode") == "open" and z and doc.zammad_ticket_id and doc.zammad_group == doc.tenant.zammad_group_paid:
        try:
            z.set_group(doc.zammad_ticket_id, doc.tenant.zammad_group_topay)
            doc.zammad_group = doc.tenant.zammad_group_topay
        except Exception:  # noqa: BLE001
            log.exception("move to toPay failed")
    try:
        pipeline.book(db, z, doc, actor=actor)
    except Exception as e:  # noqa: BLE001
        log.warning("booking failed: %s", e)
        doc.state = "error"
        doc.last_error = str(e)[:1000]
        db.commit()
        return out
    out["booked"] = doc.state == "booked"
    if out["booked"] and remember and doc.supplier:
        same = doc.rule if (doc.rule and (doc.rule.keywords or []) == keywords) else None
        rule = rules.learn_from_decision(db, doc.tenant, doc.supplier, same, dec, keywords=keywords, source="review")
        doc.rule = rule
        audit.log(db, "rule.learned", tenant_id=doc.tenant_id, actor=actor, entity="rule", entity_id=rule.id,
                  details={"supplier": doc.supplier.display_name, "auto_book": rule.auto_book})
        db.commit()
        out["rule_id"] = rule.id
    return out


def skip_document(db: Session, z: ZammadClient | None, doc: Document, kind: str, *, close_ticket: bool, actor: str) -> None:
    doc.state = {"skip": "skipped", "not_receipt": "not_a_receipt", "cancel": "cancelled"}.get(kind, "skipped")
    audit.log(db, f"document.{doc.state}", tenant_id=doc.tenant_id, actor=actor, entity="document", entity_id=doc.id)
    db.commit()
    if kind == "not_receipt" and close_ticket and doc.zammad_ticket_id and z:
        try:
            z.add_note(doc.zammad_ticket_id, "finance: Kein Beleg, Ticket geschlossen.")
            z.set_state(doc.zammad_ticket_id, "closed")
        except Exception:  # noqa: BLE001
            log.exception("close failed")


def restore_document(db: Session, doc: Document, *, actor: str) -> str:
    """Undo skip / not_a_receipt / cancel in the tool. A cancelled booking becomes ok again (bexio was never touched),
    otherwise the document goes back to the review queue."""
    if doc.booking and doc.booking.status == "cancelled":
        doc.booking.status = "ok"
        doc.state = "booked"
    elif doc.booking and doc.booking.status == "ok":
        doc.state = "booked"
    else:
        doc.state = "needs_review"
        doc.review_reason = (doc.review_reason or "") or "wiederhergestellt"
    audit.log(db, "document.restored", tenant_id=doc.tenant_id, actor=actor, entity="document", entity_id=doc.id, details={"state": doc.state})
    db.commit()
    return doc.state
