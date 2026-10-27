"""MCP endpoint (Streamable HTTP, JSON-RPC 2.0) so the tool can be driven from a chat client.

POST /mcp with "Authorization: Bearer <MCP_TOKEN>". Writing tools run only with confirm=true,
otherwise they return a preview. Nothing is deleted; rules and payment accounts are deactivated.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import __version__, auth
from ..config import get_settings
from ..db import SessionLocal
from ..models import AuditLog, Booking, Document, PaymentAccount, Rule, Supplier, Tenant
from ..services import actions, audit, bexio as bx, pain001, payments, pipeline, rules
from ..services.zammad import ZammadClient

log = logging.getLogger(__name__)
router = APIRouter()
PROTOCOL = "2025-03-26"
ACTOR = "mcp"

TENANT = {"type": "string", "description": "Mandant: KDT, TAB, DDL oder SPQ"}
CONFIRM = {"type": "boolean", "default": False, "description": "true = ausführen, sonst nur Vorschau"}


class ToolError(Exception):
    pass


# --- helpers -------------------------------------------------------------------------
def _tenant(db: Session, code: str) -> Tenant:
    t = db.query(Tenant).filter(Tenant.code == (code or "").upper()).first()
    if not t:
        raise ToolError(f"Unbekannter Mandant: {code}")
    return t


def _account(t: Tenant, no_or_id: Any) -> dict | None:
    """Resolve a chart-of-accounts entry by account number (preferred) or bexio id."""
    if no_or_id in (None, ""):
        return None
    key = str(no_or_id).strip()
    for a in (t.master_data or {}).get("accounts", []):
        if a.get("no") == key:
            return a
    for a in (t.master_data or {}).get("accounts", []):
        if str(a.get("id")) == key:
            return a
    raise ToolError(f"Konto {key} nicht in den Stammdaten von {t.code} (Stammdaten laden?)")


def _tax(t: Tenant, code_or_id: Any) -> dict | None:
    if code_or_id in (None, ""):
        return None
    key = str(code_or_id).strip()
    for x in (t.master_data or {}).get("taxes", []):
        if x.get("code") == key or str(x.get("id")) == key:
            return x
    raise ToolError(f"Steuercode {key} nicht in den Stammdaten von {t.code}")


def _payment_account(db: Session, t: Tenant, label_or_id: Any) -> PaymentAccount | None:
    if label_or_id in (None, ""):
        return None
    key = str(label_or_id).strip()
    q = db.query(PaymentAccount).filter(PaymentAccount.tenant_id == t.id)
    pa = q.filter(PaymentAccount.id == int(key)).first() if key.isdigit() else None
    pa = pa or q.filter(PaymentAccount.label.ilike(key)).first()
    if not pa:
        raise ToolError(f"Zahlkonto '{key}' bei {t.code} nicht gefunden")
    return pa


def _supplier(db: Session, t: Tenant, name: str, create: bool) -> Supplier | None:
    key = rules.normalize_name(name or "")
    if not key:
        raise ToolError("Lieferant fehlt")
    sup = rules.find_supplier_by_name(db, t.id, name)
    if not sup and create:
        sup = Supplier(tenant_id=t.id, display_name=name.strip()[:200], name_key=key, ibans=[], name_variants=[])
        db.add(sup)
        db.flush()
    return sup


def _zammad() -> ZammadClient | None:
    s = get_settings()
    return ZammadClient() if (s.zammad_url and s.zammad_token) else None


def _rule_dict(r: Rule) -> dict:
    t = r.tenant
    return {
        "id": r.id, "tenant": t.code, "supplier": r.supplier.display_name, "supplier_id": r.supplier_id,
        "keywords": r.keywords or [], "account_no": pipeline._master(t, "accounts", r.account_id).get("no"),
        "account_name": pipeline._master(t, "accounts", r.account_id).get("name"),
        "tax_code": pipeline._master(t, "taxes", r.tax_id).get("code"), "payment_mode": r.payment_mode,
        "payment_account": r.payment_account.label if r.payment_account else None, "booking_text": r.booking_text,
        "amount_min": r.amount_min, "amount_max": r.amount_max, "auto_book": r.auto_book, "active": r.active,
        "learned_from": r.learned_from, "hits": r.hits,
        "supplier_ibans": r.supplier.ibans or [], "supplier_uid": r.supplier.uid, "name_variants": r.supplier.name_variants or [],
        "address": {"street": r.supplier.street, "postcode": r.supplier.postcode, "city": r.supplier.city, "country": r.supplier.country},
        "bexio_contact_id": r.supplier.bexio_contact_id,
    }


def _doc_dict(d: Document, full: bool = False) -> dict:
    dec = d.decision or {}
    out = {
        "id": d.id, "tenant": d.tenant.code, "state": d.state, "received_at": d.received_at.isoformat() if d.received_at else None,
        "filename": d.filename, "ticket": d.zammad_ticket_number, "ticket_title": d.ticket_title, "zammad_group": d.zammad_group,
        "supplier": d.supplier.display_name if d.supplier else dec.get("supplier_name"),
        "amount": dec.get("amount"), "currency": dec.get("currency"), "amount_chf": dec.get("amount_chf"), "doc_date": dec.get("doc_date"), "due_date": dec.get("due_date"),
        "document_number": dec.get("document_number"), "payment_mode": dec.get("payment_mode"),
        "account_no": pipeline._master(d.tenant, "accounts", dec.get("account_id")).get("no"),
        "tax_code": pipeline._master(d.tenant, "taxes", dec.get("tax_id")).get("code"),
        "lines": [{"title": ln.get("title"), "amount": ln.get("amount"),
                   "account_no": pipeline._master(d.tenant, "accounts", ln.get("account_id")).get("no"),
                   "tax_code": pipeline._master(d.tenant, "taxes", ln.get("tax_id")).get("code")} for ln in dec.get("lines") or []],
        "review_reason": d.review_reason, "last_error": d.last_error, "reasons": dec.get("reasons"),
        "url": f"{get_settings().public_base_url}/docs/{d.id}",
    }
    if full:
        ex = d.extraction
        out["extraction"] = {"qr": ex.qr if ex else None, "llm": ex.llm if ex else None, "model": ex.model if ex else None, "text_excerpt": (ex.text_excerpt or "")[:3000] if ex else None}
        out["decision"] = dec
        out["rule_id"] = d.rule_id
        if d.booking:
            out["booking"] = {"kind": d.booking.kind, "bexio_bill_id": d.booking.bexio_bill_id, "bexio_entry_id": d.booking.bexio_entry_id,
                              "status": d.booking.status, "bexio_status": d.booking.bexio_status}
    return out


def _preview(what: str, data: dict) -> dict:
    return {"preview": True, "hinweis": f"Vorschau: {what}. Mit confirm=true ausführen.", **data}


# --- tools ---------------------------------------------------------------------------
TOOLS: list[dict] = []
HANDLERS: dict[str, Callable[[Session, dict], Any]] = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    def deco(fn):
        TOOLS.append({"name": name, "description": description,
                      "inputSchema": {"type": "object", "properties": properties, "required": required or []}})
        HANDLERS[name] = fn
        return fn
    return deco


@tool("get_version", "Version des laufenden finance-Servers.", {})
def t_version(db, a):
    return {"version": __version__}


@tool("list_tenants", "Mandanten mit bexio-Status und offenen Belegen.", {})
def t_tenants(db, a):
    out = []
    for t in db.query(Tenant).order_by(Tenant.id).all():
        n = db.query(Document).filter(Document.tenant_id == t.id, Document.state.in_(["needs_review", "needs_chf", "error"])).count()
        out.append({"code": t.code, "name": t.name, "active": t.active, "bexio_connected": t.bexio_connected,
                    "bexio_company": t.bexio_company_name, "bexio_company_id": t.bexio_company_id, "master_data_at": t.master_data_at.isoformat() if t.master_data_at else None,
                    "open_todo": n})
    return out


@tool("get_settings", "Einstellungen eines Mandanten: Standardkonten, Steuercode, Bankkonto, Zammad-Gruppen, Zahlkonten.", {"tenant": TENANT}, ["tenant"])
def t_get_settings(db, a):
    t = _tenant(db, a["tenant"])
    md = t.master_data or {}
    acc = lambda i: pipeline._master(t, "accounts", i).get("no")  # noqa: E731
    return {
        "tenant": t.code, "name": t.name, "active": t.active, "bexio_connected": t.bexio_connected,
        "zammad_group_paid": t.zammad_group_paid, "zammad_group_topay": t.zammad_group_topay,
        "default_expense_account_no": acc(t.default_expense_account_id), "creditor_account_no": acc(t.creditor_account_id),
        "fee_account_no": acc(t.fee_account_id), "fx_diff_account_no": acc(t.fx_diff_account_id),
        "commission_account_no": acc(t.commission_account_id),
        "default_tax_code": pipeline._master(t, "taxes", t.default_tax_id).get("code"),
        "bank_account": next(({"id": b["id"], "name": b["name"], "iban": b["iban"]} for b in md.get("bank_accounts", []) if b["id"] == t.bank_account_id), None),
        "payment_iban": t.payment_iban, "payment_name": t.payment_name,
        "bank_accounts": md.get("bank_accounts", []),
        "payment_accounts": [{"id": p.id, "label": p.label, "kind": p.kind, "card_last4": p.card_last4, "bexio_account_no": p.bexio_account_no, "active": p.active}
                             for p in db.query(PaymentAccount).filter(PaymentAccount.tenant_id == t.id).all()],
        "counts": {"accounts": len(md.get("accounts", [])), "taxes": len(md.get("taxes", []))},
    }


@tool("set_tenant_settings", "Einstellungen eines Mandanten setzen (Konten per Kontonummer, Steuer per Code, Bankkonto per bexio-ID).",
      {"tenant": TENANT, "default_expense_account_no": {"type": "string"}, "creditor_account_no": {"type": "string"},
       "fee_account_no": {"type": "string"}, "fx_diff_account_no": {"type": "string"},
       "commission_account_no": {"type": "string", "description": "Ertragskonto für Provisions-Gutschriften, z.B. 3681"}, "default_tax_code": {"type": "string"},
       "bank_account_id": {"type": "integer"}, "zammad_group_paid": {"type": "string"}, "zammad_group_topay": {"type": "string"},
       "payment_iban": {"type": "string", "description": "IBAN des zahlenden Bankkontos für Zahlfiles"}, "payment_name": {"type": "string"},
       "active": {"type": "boolean"}, "confirm": CONFIRM}, ["tenant"])
def t_set_settings(db, a):
    t = _tenant(db, a["tenant"])
    changes = {}
    for key, col in (("default_expense_account_no", "default_expense_account_id"), ("creditor_account_no", "creditor_account_id"),
                     ("fee_account_no", "fee_account_id"), ("fx_diff_account_no", "fx_diff_account_id"),
                     ("commission_account_no", "commission_account_id")):
        if key in a:
            acc = _account(t, a[key])
            changes[col] = acc["id"] if acc else None
    if "default_tax_code" in a:
        tx = _tax(t, a["default_tax_code"])
        changes["default_tax_id"] = tx["id"] if tx else None
    if "bank_account_id" in a:
        if a["bank_account_id"] is not None and not any(b["id"] == a["bank_account_id"] for b in (t.master_data or {}).get("bank_accounts", [])):
            raise ToolError("bank_account_id nicht in den Stammdaten")
        changes["bank_account_id"] = a["bank_account_id"]
    for key in ("zammad_group_paid", "zammad_group_topay"):
        if a.get(key):
            changes[key] = a[key][:80]
    if "payment_iban" in a:
        iban = rules.normalize_iban(a["payment_iban"])
        if iban and not pain001.iban_valid(iban):
            raise ToolError("payment_iban ungültig")
        changes["payment_iban"] = iban or None
    if "payment_name" in a:
        changes["payment_name"] = (a["payment_name"] or "")[:70] or None
    if "active" in a:
        changes["active"] = bool(a["active"])
    if not changes:
        raise ToolError("Keine Änderung angegeben")
    if not a.get("confirm"):
        return _preview(f"Einstellungen {t.code} ändern", {"changes": changes})
    for k, v in changes.items():
        setattr(t, k, v)
    audit.log(db, "tenant.saved", tenant_id=t.id, actor=ACTOR, entity="tenant", entity_id=t.id, details=changes)
    db.commit()
    return {"ok": True, "changes": changes}


@tool("refresh_master_data", "Kontenplan, Steuercodes, Währungen und Bankkonten aus bexio laden.", {"tenant": TENANT, "confirm": CONFIRM}, ["tenant"])
def t_refresh(db, a):
    t = _tenant(db, a["tenant"])
    if not t.bexio_connected:
        raise ToolError(f"{t.code}: bexio nicht verbunden")
    if not a.get("confirm"):
        return _preview(f"Stammdaten {t.code} aus bexio laden", {})
    c = bx.BexioClient(db, t)
    try:
        md = c.refresh_master_data()
    finally:
        c.close()
    db.commit()
    return {"ok": True, "accounts": len(md.get("accounts", [])), "taxes": len(md.get("taxes", [])), "bank_accounts": md.get("bank_accounts", [])}


@tool("learn_from_history", "Regeln aus bezahlten Kreditorenrechnungen in bexio ableiten: Konto je Lieferant, Betragsband, Zahlungsquelle (Bankkonto der Zahlung -> Zahlkonto), Lieferanten-IBAN.", {"tenant": TENANT, "confirm": CONFIRM}, ["tenant"])
def t_learn(db, a):
    t = _tenant(db, a["tenant"])
    if not t.bexio_connected:
        raise ToolError(f"{t.code}: bexio nicht verbunden")
    if not a.get("confirm"):
        return _preview(f"Regeln für {t.code} aus der bexio-Historie lernen", {})
    res = pipeline.learn_from_history(db, t)
    audit.log(db, "rules.learned_history", tenant_id=t.id, actor=ACTOR, details=res)
    db.commit()
    return {"ok": True, **res, "hinweis": "unmapped_bank_accounts: bexio-Bankkonten, aus denen Rechnungen bezahlt wurden, ohne Zahlkonto im Tool; "
                                        "mit set_payment_account (bexio_account_no des Kontos) anlegen und erneut lernen, dann bekommen diese Lieferanten card/private/bank_paid"}


@tool("list_accounts", "Kontenplan eines Mandanten (aus den Stammdaten), optional gefiltert nach Nummer oder Name.", {"tenant": TENANT, "q": {"type": "string"}}, ["tenant"])
def t_accounts(db, a):
    t = _tenant(db, a["tenant"])
    q = (a.get("q") or "").lower()
    return [x for x in (t.master_data or {}).get("accounts", []) if not q or q in x.get("no", "") or q in x.get("name", "").lower()]


@tool("list_taxes", "Steuercodes eines Mandanten.", {"tenant": TENANT}, ["tenant"])
def t_taxes(db, a):
    t = _tenant(db, a["tenant"])
    return (t.master_data or {}).get("taxes", [])


@tool("list_todo", "Belege in Prüfung, mit fehlendem CHF-Betrag oder mit Fehler (Standard), oder nach Status gefiltert.",
      {"tenant": TENANT, "state": {"type": "string", "description": "new | needs_review | needs_chf | error | booked | skipped | not_a_receipt | cancelled | extracted"},
       "limit": {"type": "integer", "default": 50}})
def t_todo(db, a):
    q = db.query(Document).join(Tenant)
    if a.get("tenant"):
        q = q.filter(Tenant.code == a["tenant"].upper())
    q = q.filter(Document.state == a["state"]) if a.get("state") else q.filter(Document.state.in_(["needs_review", "needs_chf", "error"]))
    return [_doc_dict(d) for d in q.order_by(Document.received_at.asc()).limit(int(a.get("limit") or 50)).all()]


@tool("get_document", "Beleg im Detail: Extraktion (QR, LLM), Entscheid, Gründe, Buchung.", {"id": {"type": "integer"}}, ["id"])
def t_get_doc(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    return _doc_dict(d, full=True)


@tool("book_document", "Beleg buchen: Felder überschreiben (Konto per Nummer, Steuer per Code, Zahlkonto per Label) und in bexio verbuchen. "
      "payment_mode open = offene Kreditorenrechnung, card/private/bank_paid = bezahlt (manuelle Buchung gegen Zahlkonto), "
      "credit = Provisions-Gutschrift als Debitorenrechnung (Ertragskonto, Umsatzsteuercode). "
      "lines = Positionen (z.B. je MWST-Satz), Summe muss amount ergeben; [] bucht einzeilig. "
      "Bezahlt in Fremdwährung: amount_chf = belasteter CHF-Betrag laut Abrechnung, gebucht wird in CHF. "
      "remember=true merkt sich die Entscheidung als Regel für den Lieferanten.",
      {"id": {"type": "integer"}, "supplier_name": {"type": "string"}, "account_no": {"type": "string"}, "tax_code": {"type": "string"},
       "payment_mode": {"type": "string", "enum": ["open", "card", "private", "bank_paid", "credit"]}, "payment_account": {"type": "string"},
       "booking_text": {"type": "string"}, "amount": {"type": "number"}, "currency": {"type": "string"},
       "amount_chf": {"type": "number", "description": "nur bezahlt in Fremdwährung: belasteter Betrag in CHF laut Karten-/Bankabrechnung"},
       "lines": {"type": "array", "description": "Positionen brutto; account_no leer = Aufwandkonto des Belegs",
                 "items": {"type": "object", "properties": {"title": {"type": "string"}, "amount": {"type": "number"},
                                                            "account_no": {"type": "string"}, "tax_code": {"type": "string"}}, "required": ["amount"]}}, "doc_date": {"type": "string"},
       "due_date": {"type": "string"}, "document_number": {"type": "string"}, "exchange_rate": {"type": "number", "description": "Kurs in CHF je Einheit Fremdwährung (optional, sonst BAZG-Tageskurs zum Belegdatum)"},
       "already_paid": {"type": "boolean", "default": False, "description": "offene Rechnung ist schon bezahlt: Ticket sofort schliessen, Ausgleich über Banksync (wird auch erkannt, wenn ein Agent 'bezahlt' ins Ticket geschrieben hat)"},
       "remember": {"type": "boolean", "default": False},
       "keywords": {"type": "array", "items": {"type": "string"}}, "confirm": CONFIRM}, ["id"])
def t_book(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    if d.state == "booked":
        raise ToolError("Beleg ist bereits gebucht")
    t = d.tenant
    dec = dict(d.decision or {})
    if a.get("supplier_name"):
        dec["supplier_name"] = a["supplier_name"].strip()
    if "account_no" in a:
        acc = _account(t, a["account_no"])
        dec["account_id"] = acc["id"] if acc else None
    if "tax_code" in a:
        tx = _tax(t, a["tax_code"])
        dec["tax_id"] = tx["id"] if tx else None
    if a.get("payment_mode"):
        dec["payment_mode"] = a["payment_mode"]
    if "payment_account" in a:
        pa = _payment_account(db, t, a["payment_account"])
        dec["payment_account_id"] = pa.id if pa else None
    for k in ("booking_text", "amount", "currency", "amount_chf", "doc_date", "due_date", "document_number", "exchange_rate", "already_paid"):
        if k in a and a[k] is not None:
            dec[k] = a[k]
    if "lines" in a:
        lines = []
        for ln in a.get("lines") or []:
            acc = _account(t, ln["account_no"]) if ln.get("account_no") else None
            if ln.get("account_no") and not acc:
                raise ToolError(f"Konto {ln['account_no']} unbekannt")
            tx = _tax(t, ln["tax_code"]) if ln.get("tax_code") else None
            if ln.get("tax_code") and not tx:
                raise ToolError(f"Steuercode {ln['tax_code']} unbekannt")
            lines.append({"title": (ln.get("title") or "")[:80], "amount": round(float(ln["amount"]), 2),
                          "account_id": acc["id"] if acc else None, "tax_id": tx["id"] if tx else None})
        dec["lines"] = rules.with_net(t, lines) if len(lines) > 1 else []
    dec["payment_mode"] = dec.get("payment_mode") or "open"
    if dec["payment_mode"] in ("open", "credit"):
        dec["payment_account_id"] = None
    missing = [k for k in ("account_id", "amount", "doc_date") if not dec.get(k)]
    if dec["payment_mode"] not in ("open", "credit") and not dec.get("payment_account_id"):
        missing.append("payment_account")
    if actions.needs_chf(dec) and not dec.get("amount_chf"):
        missing.append("amount_chf")
    if dec.get("lines") and dec.get("amount") and abs(sum(ln["amount"] for ln in dec["lines"]) - float(dec["amount"])) > 0.005:
        raise ToolError(f"Positionen ergeben {sum(ln['amount'] for ln in dec['lines']):.2f}, Betrag ist {float(dec['amount']):.2f}")
    if not dec.get("supplier_name") and not d.supplier:
        missing.append("supplier_name")
    summary = {
        "document": d.id, "tenant": t.code, "supplier": dec.get("supplier_name") or (d.supplier.display_name if d.supplier else None),
        "amount": dec.get("amount"), "currency": dec.get("currency", "CHF"), "doc_date": dec.get("doc_date"), "due_date": dec.get("due_date"),
        "account_no": pipeline._master(t, "accounts", dec.get("account_id")).get("no"), "tax_code": pipeline._master(t, "taxes", dec.get("tax_id")).get("code"),
        "payment_mode": dec["payment_mode"], "payment_account_id": dec.get("payment_account_id"), "amount_chf": dec.get("amount_chf"),
        "lines": [{"amount": ln["amount"], "account_no": pipeline._master(t, "accounts", ln.get("account_id")).get("no"),
                   "tax_code": pipeline._master(t, "taxes", ln.get("tax_id")).get("code")} for ln in dec.get("lines") or []], "remember": bool(a.get("remember")),
    }
    if missing:
        raise ToolError("Felder fehlen: " + ", ".join(missing))
    if not a.get("confirm"):
        return _preview("Beleg buchen", summary)
    z = _zammad()
    try:
        result = actions.book_with_decision(db, z, d, dec, remember=bool(a.get("remember")), keywords=list(a.get("keywords") or []), actor=ACTOR)
    finally:
        if z:
            z.close()
    return {"ok": d.state == "booked", "state": d.state, "error": d.last_error if d.state != "booked" else None, **result}


@tool("skip_document", "Beleg überspringen (skip), als 'kein Beleg' markieren (not_receipt, optional Ticket schliessen) oder stornieren (cancel).",
      {"id": {"type": "integer"}, "kind": {"type": "string", "enum": ["skip", "not_receipt", "cancel"], "default": "skip"},
       "close_ticket": {"type": "boolean", "default": False}, "confirm": CONFIRM}, ["id"])
def t_skip(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    kind = a.get("kind") or "skip"
    if not a.get("confirm"):
        return _preview(f"Beleg {d.id} -> {kind}", {"close_ticket": bool(a.get("close_ticket"))})
    z = _zammad()
    try:
        actions.skip_document(db, z, d, kind, close_ticket=bool(a.get("close_ticket")), actor=ACTOR)
    finally:
        if z:
            z.close()
    return {"ok": True, "state": d.state}


@tool("restore_document", "Überspringen / 'kein Beleg' / Stornierung im Tool rückgängig machen (bexio wird nicht angefasst).",
      {"id": {"type": "integer"}, "confirm": CONFIRM}, ["id"])
def t_restore(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    if not a.get("confirm"):
        return _preview(f"Beleg {d.id} ({d.state}) wiederherstellen", {})
    state = actions.restore_document(db, d, actor=ACTOR)
    return {"ok": True, "state": state}


@tool("reprocess_document", "Beleg neu auslesen und neu entscheiden (QR, LLM, Regeln).", {"id": {"type": "integer"}, "confirm": CONFIRM}, ["id"])
def t_reprocess(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    if not a.get("confirm"):
        return _preview(f"Beleg {d.id} neu verarbeiten", {})
    z = _zammad()
    try:
        pipeline.process_document(db, z, d)
    finally:
        if z:
            z.close()
    return _doc_dict(d, full=True)


@tool("list_rules", "Regeln je Lieferant, optional nach Mandant und Namensfilter.", {"tenant": TENANT, "q": {"type": "string"}, "include_inactive": {"type": "boolean", "default": False}})
def t_rules(db, a):
    q = db.query(Rule).join(Supplier).join(Tenant, Rule.tenant_id == Tenant.id)
    if a.get("tenant"):
        q = q.filter(Tenant.code == a["tenant"].upper())
    if a.get("q"):
        q = q.filter(Supplier.display_name.ilike(f"%{a['q']}%"))
    if not a.get("include_inactive"):
        q = q.filter(Rule.active.is_(True))
    return [_rule_dict(r) for r in q.order_by(Supplier.display_name).limit(1000).all()]


RULE_PROPS = {
    "supplier_name": {"type": "string", "description": "Lieferant (wird angelegt, falls neu)"},
    "account_no": {"type": "string", "description": "Aufwandkonto, z.B. 6510"}, "tax_code": {"type": "string", "description": "Steuercode, z.B. VM81"},
    "payment_mode": {"type": "string", "enum": ["open", "card", "private", "bank_paid", "credit"]},
    "payment_account": {"type": "string", "description": "Label oder ID des Zahlkontos (bei card/private/bank_paid)"},
    "booking_text": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}, "description": "leer = Standardregel des Lieferanten"},
    "amount_min": {"type": "number"}, "amount_max": {"type": "number"}, "auto_book": {"type": "boolean"}, "active": {"type": "boolean"},
    "ibans": {"type": "array", "items": {"type": "string"}}, "uid": {"type": "string", "description": "CHE-Nummer"},
    "name_variants": {"type": "array", "items": {"type": "string"}}, "bexio_contact_id": {"type": "integer"},
    "street": {"type": "string", "description": "Postadresse des Lieferanten (für Zahlfiles)"}, "postcode": {"type": "string"}, "city": {"type": "string"}, "country": {"type": "string", "description": "ISO-2"},
}


def _upsert_rule(db: Session, t: Tenant, spec: dict) -> tuple[Rule, bool, dict]:
    sup = _supplier(db, t, spec.get("supplier_name") or "", create=True)
    keywords = [k.strip() for k in (spec.get("keywords") or []) if k.strip()]
    # a commission rule (credit) lives beside the invoice rule of the same partner: match on that when the mode is given
    credit = spec.get("payment_mode") == "credit" if spec.get("payment_mode") else None
    rule = next((r for r in sup.rules if (r.keywords or []) == keywords and (credit is None or (r.payment_mode == "credit") == credit)), None)
    created = rule is None
    if created:
        rule = Rule(tenant_id=t.id, supplier_id=sup.id, keywords=keywords, learned_from="mcp", payment_mode="open", hits=0)
        db.add(rule)
    changes = {}
    if "account_no" in spec:
        acc = _account(t, spec["account_no"])
        rule.account_id = acc["id"] if acc else None
        changes["account_no"] = acc["no"] if acc else None
    if "tax_code" in spec:
        tx = _tax(t, spec["tax_code"])
        rule.tax_id = tx["id"] if tx else None
        changes["tax_code"] = tx["code"] if tx else None
    if spec.get("payment_mode"):
        rule.payment_mode = spec["payment_mode"]
        changes["payment_mode"] = rule.payment_mode
    if "payment_account" in spec:
        pa = _payment_account(db, t, spec["payment_account"])
        rule.payment_account_id = pa.id if pa else None
        changes["payment_account"] = pa.label if pa else None
    if rule.payment_mode in ("open", "credit"):
        rule.payment_account_id = None
    for k in ("booking_text", "amount_min", "amount_max", "auto_book", "active"):
        if k in spec:
            setattr(rule, k, spec[k])
            changes[k] = spec[k]
    if rule.active is None:
        rule.active = True
    if rule.auto_book is None:
        rule.auto_book = False
    if "ibans" in spec:
        sup.ibans = [rules.normalize_iban(x) for x in spec["ibans"] if x]
        changes["ibans"] = sup.ibans
    if "uid" in spec:
        sup.uid = rules.normalize_uid(spec["uid"]) or None
        changes["uid"] = sup.uid
    if "name_variants" in spec:
        sup.name_variants = [x.strip() for x in spec["name_variants"] if x.strip()]
        changes["name_variants"] = sup.name_variants
    if "bexio_contact_id" in spec:
        sup.bexio_contact_id = spec["bexio_contact_id"]
        changes["bexio_contact_id"] = sup.bexio_contact_id
    for k in ("street", "postcode", "city", "country"):
        if k in spec:
            v = (spec[k] or "").strip()
            setattr(sup, k, (v.upper()[:2] if k == "country" else v[:100]) or None)
            changes[k] = getattr(sup, k)
    if not created:
        rule.learned_from = "mcp"
    db.flush()
    return rule, created, changes


@tool("upsert_rule", "Regel für einen Lieferanten anlegen oder ändern (Standardregel ohne keywords, oder Keyword-Regel).",
      {"tenant": TENANT, **RULE_PROPS, "confirm": CONFIRM}, ["tenant", "supplier_name"])
def t_upsert_rule(db, a):
    t = _tenant(db, a["tenant"])
    if not a.get("confirm"):
        _account(t, a.get("account_no"))
        _tax(t, a.get("tax_code"))
        if a.get("payment_account"):
            _payment_account(db, t, a["payment_account"])
        sup = _supplier(db, t, a.get("supplier_name") or "", create=False)
        return _preview("Regel speichern", {"supplier_exists": sup is not None, "spec": {k: v for k, v in a.items() if k not in ("confirm",)}})
    rule, created, changes = _upsert_rule(db, t, a)
    audit.log(db, "rule.saved", tenant_id=t.id, actor=ACTOR, entity="rule", entity_id=rule.id, details=changes)
    db.commit()
    return {"ok": True, "created": created, "rule": _rule_dict(rule)}


@tool("upsert_rules", "Mehrere Regeln auf einmal anlegen oder ändern (z.B. aus einer Auswertung des bexio-Journals).",
      {"tenant": TENANT, "rules": {"type": "array", "items": {"type": "object", "properties": RULE_PROPS, "required": ["supplier_name"]}}, "confirm": CONFIRM},
      ["tenant", "rules"])
def t_upsert_rules(db, a):
    t = _tenant(db, a["tenant"])
    specs = list(a.get("rules") or [])
    if not specs:
        raise ToolError("rules ist leer")
    if not a.get("confirm"):
        problems = []
        for s in specs:
            try:
                _account(t, s.get("account_no"))
                _tax(t, s.get("tax_code"))
                if s.get("payment_account"):
                    _payment_account(db, t, s["payment_account"])
            except ToolError as e:
                problems.append({"supplier_name": s.get("supplier_name"), "error": str(e)})
        return _preview(f"{len(specs)} Regeln für {t.code} speichern", {"problems": problems, "rules": specs})
    out = []
    for s in specs:
        try:
            rule, created, changes = _upsert_rule(db, t, s)
            out.append({"supplier": rule.supplier.display_name, "rule_id": rule.id, "created": created, "changes": changes})
        except ToolError as e:
            out.append({"supplier": s.get("supplier_name"), "error": str(e)})
    audit.log(db, "rules.upserted", tenant_id=t.id, actor=ACTOR, details={"count": len(out)})
    db.commit()
    return {"ok": True, "results": out}


@tool("deactivate_rule", "Regel deaktivieren (wird nicht gelöscht).", {"rule_id": {"type": "integer"}, "confirm": CONFIRM}, ["rule_id"])
def t_deactivate_rule(db, a):
    r = db.get(Rule, int(a["rule_id"]))
    if not r:
        raise ToolError("Regel nicht gefunden")
    if not a.get("confirm"):
        return _preview(f"Regel {r.id} ({r.supplier.display_name}) deaktivieren", {})
    r.active = False
    audit.log(db, "rule.deactivated", tenant_id=r.tenant_id, actor=ACTOR, entity="rule", entity_id=r.id)
    db.commit()
    return {"ok": True}


@tool("merge_suppliers", "Doppelten Lieferanten zusammenführen: Belege, Regeln, IBANs, Namensvarianten und Adresse von "
      "source_supplier_id nach target_supplier_id verschieben. Der Quell-Lieferant bleibt leer zurück (nichts wird gelöscht).",
      {"source_supplier_id": {"type": "integer"}, "target_supplier_id": {"type": "integer"}, "confirm": CONFIRM},
      ["source_supplier_id", "target_supplier_id"])
def t_merge_suppliers(db, a):
    src, dst = db.get(Supplier, int(a["source_supplier_id"])), db.get(Supplier, int(a["target_supplier_id"]))
    if not src or not dst:
        raise ToolError("Lieferant nicht gefunden")
    if src.tenant_id != dst.tenant_id or src.id == dst.id:
        raise ToolError("Lieferanten müssen verschieden sein und zum gleichen Mandanten gehören")
    if not a.get("confirm"):
        return _preview(f"{src.display_name} (#{src.id}) in {dst.display_name} (#{dst.id}) zusammenführen",
                        {"rules": [r.id for r in src.rules], "ibans": src.ibans or []})
    out = rules.merge_suppliers(db, src, dst)
    audit.log(db, "supplier.merged", tenant_id=dst.tenant_id, actor=ACTOR, entity="supplier", entity_id=dst.id,
              details={"source": src.id, **out})
    db.commit()
    return {"ok": True, **out}


@tool("list_payment_accounts", "Zahlkonten (Bank, Kreditkarten, privat) je Mandant.", {"tenant": TENANT})
def t_payment_accounts(db, a):
    q = db.query(PaymentAccount).join(Tenant)
    if a.get("tenant"):
        q = q.filter(Tenant.code == a["tenant"].upper())
    return [{"id": p.id, "tenant": p.tenant.code, "label": p.label, "kind": p.kind, "card_last4": p.card_last4,
             "bexio_account_no": p.bexio_account_no, "bexio_account_id": p.bexio_account_id, "active": p.active} for p in q.order_by(Tenant.id, PaymentAccount.label).all()]


@tool("set_payment_account", "Zahlkonto anlegen oder ändern (Gegenkonto per Kontonummer aus dem Kontenplan).",
      {"tenant": TENANT, "id": {"type": "integer", "description": "bestehendes Zahlkonto ändern"}, "label": {"type": "string"},
       "kind": {"type": "string", "enum": ["bank", "card", "private"]}, "card_last4": {"type": "string"}, "bexio_account_no": {"type": "string"},
       "active": {"type": "boolean"}, "confirm": CONFIRM}, ["tenant"])
def t_set_payment_account(db, a):
    t = _tenant(db, a["tenant"])
    pa = db.get(PaymentAccount, int(a["id"])) if a.get("id") else None
    if a.get("id") and (not pa or pa.tenant_id != t.id):
        raise ToolError("Zahlkonto nicht gefunden")
    if not pa and not (a.get("label") and a.get("kind") and a.get("bexio_account_no")):
        raise ToolError("Für ein neues Zahlkonto braucht es label, kind und bexio_account_no")
    acc = _account(t, a.get("bexio_account_no")) if "bexio_account_no" in a else None
    changes = {k: a[k] for k in ("label", "kind", "card_last4", "active") if k in a}
    if acc:
        changes["bexio_account_no"] = acc["no"]
    if not a.get("confirm"):
        return _preview("Zahlkonto speichern", {"existing": pa.label if pa else None, "changes": changes})
    pa = pa or PaymentAccount(tenant_id=t.id, active=True)
    for k in ("label", "kind", "active"):
        if k in a:
            setattr(pa, k, a[k])
    if "card_last4" in a:
        pa.card_last4 = (a["card_last4"] or "")[-4:] or None
    if acc:
        pa.bexio_account_id, pa.bexio_account_no = acc["id"], acc["no"]
    db.add(pa)
    audit.log(db, "payment_account.saved", tenant_id=t.id, actor=ACTOR, entity="payment_account", details=changes)
    db.commit()
    return {"ok": True, "id": pa.id, "label": pa.label}


@tool("list_bookings", "Buchungen (bexio-Rechnungen und manuelle Buchungen), neueste zuerst.", {"tenant": TENANT, "limit": {"type": "integer", "default": 50}})
def t_bookings(db, a):
    q = db.query(Booking).join(Tenant)
    if a.get("tenant"):
        q = q.filter(Tenant.code == a["tenant"].upper())
    return [{"id": b.id, "tenant": b.tenant.code, "document_id": b.document_id, "kind": b.kind, "amount": b.amount, "currency": b.currency,
             "bexio_bill_id": b.bexio_bill_id, "bexio_entry_id": b.bexio_entry_id, "status": b.status, "bexio_status": b.bexio_status,
             "booked_at": b.booked_at.isoformat() if b.booked_at else None, "paid_at": b.paid_at.isoformat() if b.paid_at else None}
            for b in q.order_by(Booking.booked_at.desc()).limit(int(a.get("limit") or 50)).all()]


@tool("get_audit_log", "Audit-Log, neueste zuerst.", {"tenant": TENANT, "limit": {"type": "integer", "default": 50}})
def t_audit(db, a):
    q = db.query(AuditLog)
    if a.get("tenant"):
        t = _tenant(db, a["tenant"])
        q = q.filter(AuditLog.tenant_id == t.id)
    return [{"at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action, "entity": r.entity, "entity_id": r.entity_id, "details": r.details}
            for r in q.order_by(AuditLog.id.desc()).limit(int(a.get("limit") or 50)).all()]


@tool("bexio_bills", "Kreditorenrechnungen direkt aus bexio lesen (nur lesend): Filter status DRAFTS|TODO|PAID|OVERDUE, vendor (Lieferantenname), vendor_ref, document_no.",
      {"tenant": TENANT, "status": {"type": "string"}, "vendor": {"type": "string"}, "vendor_ref": {"type": "string"}, "document_no": {"type": "string"},
       "limit": {"type": "integer", "default": 20}}, ["tenant"])
def t_bexio_bills(db, a):
    t = _tenant(db, a["tenant"])
    if not t.bexio_connected:
        raise ToolError(f"{t.code}: bexio nicht verbunden")
    params = {k: a[k] for k in ("status", "vendor", "vendor_ref", "document_no") if a.get(k)}
    params["limit"] = int(a.get("limit") or 20)
    c = bx.BexioClient(db, t)
    try:
        res = c.list_bills(**params)
    finally:
        c.close()
    rows = res.get("data", []) if isinstance(res, dict) else res
    return [{k: b.get(k) for k in ("id", "document_no", "vendor_ref", "title", "status", "vendor", "bill_date", "due_date", "currency_code", "gross", "net",
                                   "pending_amount", "booking_account_ids")} for b in rows]


@tool("bexio_bill", "Eine Kreditorenrechnung im Detail aus bexio lesen (Positionen mit Konto und Steuer, Zahlung, Anhänge).",
      {"tenant": TENANT, "bill_id": {"type": "string", "description": "bexio Bill-UUID"}}, ["tenant", "bill_id"])
def t_bexio_bill(db, a):
    t = _tenant(db, a["tenant"])
    c = bx.BexioClient(db, t)
    try:
        bill = c.get_bill(a["bill_id"])
    finally:
        c.close()
    for li in bill.get("line_items", []):
        li["account_no"] = pipeline._master(t, "accounts", li.get("booking_account_id")).get("no")
        li["tax_code"] = pipeline._master(t, "taxes", li.get("tax_id")).get("code")
    return bill


@tool("bexio_duplicate_contacts", "Doppelte Kontakte in bexio finden (gleicher Name, Schreibweise egal). Pro Kontakt: offene und bezahlte "
      "Kreditorenrechnungen, Ausgaben, Verkaufsdokumente und die geplante Aktion (main = bleibt, archive = archivieren, "
      "move = offene Rechnungen auf Hauptkontakt umhängen und archivieren, mark = als Duplikat kennzeichnen). already_archived: "
      "bereits archivierte Kontakte mit gleichem Namen (die Analyse vergleicht nur aktive Kontakte). Nur lesend.",
      {"tenant": TENANT}, ["tenant"])
def t_bexio_duplicates(db, a):
    from ..services import contact_dedup

    t = _tenant(db, a["tenant"])
    c = bx.BexioClient(db, t)
    try:
        groups = contact_dedup.analyze(db, c, t)
    finally:
        c.close()
    summary = {k: sum(1 for g in groups for r in g["contacts"] if r["action"] == k) for k in ("archive", "move", "mark")}
    return {"tenant": t.code, "groups": len(groups), "summary": summary, "duplicates": groups}


@tool("clean_duplicate_contacts", "Doppelte bexio-Kontakte bereinigen gemäss bexio_duplicate_contacts. keys: nur diese Gruppen (Schlüssel aus der Analyse), "
      "leer = alle. main: {Schlüssel: Kontakt-ID} legt den Hauptkontakt einer Gruppe abweichend fest. archive_paid: Duplikate mit bezahlten "
      "Rechnungen oder Ausgaben archivieren statt nur markieren (die Belege bleiben am archivierten Kontakt). Ohne confirm nur Vorschau.",
      {"tenant": TENANT, "keys": {"type": "array", "items": {"type": "string"}},
       "main": {"type": "object", "additionalProperties": {"type": "integer"}},
       "sales_checked": {"type": "array", "items": {"type": "integer"}, "description": "Kontakt-IDs, bei denen ausserhalb von finance geprüft wurde, dass keine Verkaufsdokumente daran hängen"},
       "pair": {"type": "object", "properties": {"main_contact_id": {"type": "integer"}, "duplicate_contact_id": {"type": "integer"}},
                "description": "Einzelnes Duplikat mit abweichendem Namen (wird von der Analyse nicht gefunden) statt der Analyse-Gruppen"},
       "archive_paid": {"type": "boolean", "default": False,
                        "description": "Duplikate mit bezahlten Kreditorenrechnungen oder Ausgaben archivieren statt markieren; Verkaufsdokumente führen weiterhin zu mark"},
       "confirm": CONFIRM}, ["tenant"])
def t_clean_duplicates(db, a):
    from ..services import contact_dedup

    t = _tenant(db, a["tenant"])
    c = bx.BexioClient(db, t)
    try:
        if a.get("pair"):
            m, d = int(a["pair"]["main_contact_id"]), int(a["pair"]["duplicate_contact_id"])
            rows = []
            for cid in (m, d):
                ct, is_archived = contact_dedup.fetch_contact(c, cid)
                if is_archived and cid == m:
                    raise ToolError(f"Hauptkontakt {m} ist archiviert")
                rows.append({"id": cid, "nr": ct.get("nr"), "name": contact_dedup.contact_name(ct), "archived": is_archived, "used_by_finance": False,
                             **contact_dedup.linked(c, cid)})
            groups = [{"key": f"pair:{m}:{d}", "main_contact_id": m, "contacts": rows}]
        else:
            groups = contact_dedup.analyze(db, c, t)
        if a.get("keys"):
            groups = [g for g in groups if g["key"] in a["keys"]]
        checked = {int(x) for x in (a.get("sales_checked") or [])}
        mains = {k: int(v) for k, v in (a.get("main") or {}).items()}
        for g in groups:
            try:
                contact_dedup.plan(g, mains.get(g["key"], g["main_contact_id"]), checked, bool(a.get("archive_paid")))
            except ValueError as e:
                raise ToolError(str(e)) from e
        if not a.get("confirm"):
            return _preview(f"{len(groups)} Duplikatgruppen in {t.code} bereinigen",
                            {"groups": [{"key": g["key"], "main": g["main_contact_id"],
                                         "contacts": [{k: r.get(k) for k in ("id", "name", "archived", "bills_paid", "expenses", "action")} for r in g["contacts"]]}
                                        for g in groups]})
        results = []
        for g in groups:
            results.extend(contact_dedup.clean_group(db, c, t, g))
    finally:
        c.close()
    audit.log(db, "bexio.duplicates_cleaned", tenant_id=t.id, actor=ACTOR, entity="tenant", entity_id=t.id, details={"results": results})
    db.commit()
    return {"ok": all(r["ok"] for r in results), "results": results}


@tool("test_notification", "Pushover-Testnachricht senden (an Kims eigenes Gerät).", {})
def t_test_notification(db, a):
    from ..services.notify import pushover

    return {"sent": pushover("finance: Test", "Pushover-Anbindung funktioniert.")}


@tool("fix_open_bill", "Offene bexio-Rechnung eines Belegs korrigieren und verbuchen: Steuercode und/oder Konto auf den Positionen setzen, danach Status BOOKED. "
      "Für Belege, deren Rechnung in bexio noch als Entwurf (DRAFT) steht, z.B. nach PURCHASES.VALIDATION.TAX_INCORRECT.",
      {"id": {"type": "integer", "description": "Beleg-ID"}, "tax_code": {"type": "string"}, "account_no": {"type": "string"},
       "exchange_rate": {"type": "number", "description": "Kurs CHF je Einheit Fremdwährung; base_currency_amount wird neu gerechnet"}, "confirm": CONFIRM}, ["id"])
def t_fix_open_bill(db, a):
    d = db.get(Document, int(a["id"]))
    if not d or not d.booking or d.booking.kind != "bill_open" or not d.booking.bexio_bill_id:
        raise ToolError("Beleg hat keine offene bexio-Rechnung")
    t = d.tenant
    tx = _tax(t, a.get("tax_code")) if a.get("tax_code") else None
    acc = _account(t, a.get("account_no")) if a.get("account_no") else None
    rate = float(a["exchange_rate"]) if a.get("exchange_rate") else None
    if not a.get("confirm"):
        return _preview(f"Rechnung {d.booking.bexio_bill_id} anpassen und buchen", {"tax_code": tx["code"] if tx else None, "account_no": acc["no"] if acc else None, "exchange_rate": rate})
    c = bx.BexioClient(db, t)
    try:
        bill = c.get_bill(d.booking.bexio_bill_id)
        if tx or acc or rate:
            if str(bill.get("status", "")).upper() == "BOOKED":
                # booked bills do not take changes to lines or exchange rate; back to draft first
                c.set_bill_status(d.booking.bexio_bill_id, "DRAFT")
                bill = c.get_bill(d.booking.bexio_bill_id)
            body = c.bill_update_payload(bill)
            for li in body["line_items"]:
                if tx:
                    li["tax_id"] = tx["id"]
                if acc:
                    li["booking_account_id"] = acc["id"]
            if rate:
                body["exchange_rate"] = round(rate, 10)
                body["base_currency_amount"] = round(float(bill.get("amount_calc") or bill.get("amount_man") or 0) * rate, 2)
            bill = c.update_bill(d.booking.bexio_bill_id, body)
        if str(bill.get("status", "")).upper() == "DRAFT":
            c.set_bill_status(d.booking.bexio_bill_id, "BOOKED")
            bill = c.get_bill(d.booking.bexio_bill_id)
    finally:
        c.close()
    d.booking.bexio_status = bill.get("status")
    d.booking.response = bill
    dec = dict(d.decision or {})
    if tx:
        dec["tax_id"] = tx["id"]
    if acc:
        dec["account_id"] = acc["id"]
    if rate:
        dec["exchange_rate"] = rate
    d.decision = dec
    audit.log(db, "booking.fixed", tenant_id=t.id, actor=ACTOR, entity="booking", entity_id=d.booking.id, details={"tax_code": tx["code"] if tx else None, "account_no": acc["no"] if acc else None, "status": bill.get("status")})
    db.commit()
    return {"ok": True, "bexio_status": bill.get("status"), "bill_id": d.booking.bexio_bill_id}


@tool("list_open_payments", "Offene Kreditorenrechnungen aus dem Tool, die noch zu zahlen sind (mit IBAN, Referenz, Mitteilung und Problemen); include_in_file=true zeigt auch die, die schon in einem Zahlfile sind.",
      {"tenant": TENANT, "include_in_file": {"type": "boolean", "default": False}}, ["tenant"])
def t_open_payments(db, a):
    t = _tenant(db, a["tenant"])
    return payments.open_bills(db, t, include_in_file=bool(a.get("include_in_file")))


@tool("create_payment_file", "Zahlfile (pain.001.001.09, Swiss Payment Standard) für offene Rechnungen erstellen; die Rechnungen werden als 'im Zahlfile' markiert, die Tickets bekommen eine Notiz und gehen auf Warten (pending reminder, 14 Tage). "
      "Ohne booking_ids werden alle zahlbaren offenen Rechnungen des Mandanten genommen. Download-URL kommt zurück.",
      {"tenant": TENANT, "booking_ids": {"type": "array", "items": {"type": "integer"}}, "execution_date": {"type": "string", "description": "YYYY-MM-DD, Standard heute"}, "confirm": CONFIRM}, ["tenant"])
def t_create_payment_file(db, a):
    from datetime import datetime as _dt

    t = _tenant(db, a["tenant"])
    rows = payments.open_bills(db, t)
    ids = list(a.get("booking_ids") or [r["booking_id"] for r in rows if not r["problems"]])
    exec_date = _dt.strptime(a["execution_date"], "%Y-%m-%d").date() if a.get("execution_date") else None
    chosen = [r for r in rows if r["booking_id"] in set(ids)]
    if not a.get("confirm"):
        return _preview(f"Zahlfile {t.code} mit {len(chosen)} Zahlungen erstellen", {"payments": chosen, "execution_date": exec_date.isoformat() if exec_date else "heute",
                                                                                   "payment_iban": t.payment_iban})
    z = _zammad()
    try:
        name, data, used = payments.create_file(db, z, t, ids, exec_date, actor=ACTOR)
    except ValueError as e:
        raise ToolError(str(e))
    finally:
        if z:
            z.close()
    return {"ok": True, "file": name, "count": len(used), "total": round(sum(float(r["amount"]) for r in used), 2),
            "download": f"{get_settings().public_base_url}/payments/files/{t.code}/{name}", "payments": [{k: r[k] for k in ("supplier", "amount", "currency", "document_number")} for r in used]}


@tool("unmark_payment", "Zahlfile einer Rechnung zurückziehen (nur wenn es nicht hochgeladen wurde): alle Zahlungen des Files werden wieder offen, das File wird gesperrt "
      "(kein Download mehr), die Tickets bekommen eine Notiz und werden geöffnet. Verweigert, wenn eine Rechnung des Files in bexio bezahlt ist.",
      {"booking_id": {"type": "integer"}, "confirm": CONFIRM}, ["booking_id"])
def t_unmark_payment(db, a):
    b = db.get(Booking, int(a["booking_id"]))
    if not b:
        raise ToolError("Buchung nicht gefunden")
    if not a.get("confirm"):
        same = db.query(Booking).filter(Booking.tenant_id == b.tenant_id, Booking.payment_file == b.payment_file).all() if b.payment_file else []
        return _preview(f"Zahlfile {b.payment_file} zurückziehen und sperren. Nur wenn es nicht hochgeladen wurde, sonst wird doppelt bezahlt.",
                        {"bookings": [x.id for x in same]})
    z = _zammad()
    try:
        released = payments.unmark(db, z, b, actor=ACTOR)
    except ValueError as e:
        raise ToolError(str(e))
    finally:
        if z:
            z.close()
    return {"ok": True, "released_bookings": released}


@tool("replace_payment_file", "Zahlfile einer Rechnung neu erzeugen (nur wenn das alte nicht hochgeladen wurde): neues File mit den aktuellen Daten, altes gesperrt. "
      "remove=true nimmt diese Rechnung heraus (ihr Ticket geht wieder auf offen), die übrigen kommen ins neue File. "
      "Verweigert, wenn eine Rechnung des Files in bexio bezahlt ist oder eine verbleibende Zahlung nicht zahlbar ist.",
      {"booking_id": {"type": "integer"}, "remove": {"type": "boolean", "default": False},
       "execution_date": {"type": "string", "description": "YYYY-MM-DD, Standard heute"}, "confirm": CONFIRM}, ["booking_id"])
def t_replace_payment_file(db, a):
    from datetime import datetime as _dt

    b = db.get(Booking, int(a["booking_id"]))
    if not b or not b.payment_file:
        raise ToolError("Buchung nicht gefunden oder in keinem Zahlfile")
    remove = bool(a.get("remove"))
    exec_date = _dt.strptime(a["execution_date"], "%Y-%m-%d").date() if a.get("execution_date") else None
    if not a.get("confirm"):
        same = db.query(Booking).filter(Booking.tenant_id == b.tenant_id, Booking.payment_file == b.payment_file).all()
        keep = [x.id for x in same if not (remove and x.id == b.id)]
        return _preview(f"Zahlfile {b.payment_file} sperren und neu erzeugen" + (f" ohne Buchung {b.id}" if remove else "")
                        + ". Nur wenn das alte nicht hochgeladen wurde, sonst wird doppelt bezahlt.", {"kept_bookings": keep, "removed_booking": b.id if remove else None})
    z = _zammad()
    try:
        res = payments.replace_file(db, z, b, actor=ACTOR, remove=remove, execution_date=exec_date)
    except ValueError as e:
        raise ToolError(str(e))
    finally:
        if z:
            z.close()
    t = db.get(Tenant, b.tenant_id)
    if res["new"]:
        res["download"] = f"{get_settings().public_base_url}/payments/files/{t.code}/{res['new']}"
    return {"ok": True, **res}


# --- JSON-RPC transport ---------------------------------------------------------------
def _authorized(request: Request) -> bool:
    token = get_settings().mcp_token
    if not token:
        return False
    auth_header = request.headers.get("authorization", "")
    ok = auth_header.lower().startswith("bearer ") and hmac.compare_digest(auth_header[7:].strip(), token)
    if not ok:
        auth.mcp_failed()
    return ok


def _unauthorized(request: Request) -> JSONResponse:
    """401 normally; 429 (and a short delay) once too many wrong tokens were seen. Valid tokens are never throttled."""
    if auth.mcp_blocked():
        time.sleep(1)
        return JSONResponse({"error": "too_many_requests"}, status_code=429, headers={"Retry-After": str(auth.MCP_FAIL_WINDOW)})
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def _rpc_error(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=status)


def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": id_, "result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "finance", "version": __version__},
                                                       "instructions": "finance verarbeitet Belege aus Zammad und bucht sie in bexio. Schreibende Werkzeuge "
                                                                       "liefern ohne confirm=true nur eine Vorschau. Konten werden per Kontonummer, Steuern per Code, "
                                                                       "Zahlkonten per Label angesprochen. Es wird nichts gelöscht."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        db = SessionLocal()
        try:
            result = fn(db, args)
            text = json.dumps(result, ensure_ascii=False, default=str, indent=1)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except ToolError as e:
            db.rollback()
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": f"Fehler: {e}"}], "isError": True}}
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception("mcp tool %s failed", name)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": f"Fehler: {type(e).__name__}: {str(e)[:800]}"}], "isError": True}}
        finally:
            db.close()
    if method and method.startswith("notifications/"):
        return None
    if id_ is None:
        return None
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": f"method not found: {method}"}}


@router.post("/mcp")
async def mcp_post(request: Request):
    if not _authorized(request):
        return _unauthorized(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc_error(None, -32700, "parse error", 400)
    if isinstance(body, list):
        out = [r for r in (_handle(m) for m in body if isinstance(m, dict)) if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "invalid request", 400)
    res = _handle(body)
    if res is None:
        return Response(status_code=202)
    headers = {}
    if body.get("method") == "initialize":
        # stateless server; a session id is returned anyway because some clients expect one
        headers["Mcp-Session-Id"] = secrets.token_hex(16)
    return JSONResponse(res, headers=headers)


@router.get("/mcp")
async def mcp_get(request: Request):
    if not _authorized(request):
        return _unauthorized(request)
    return Response(status_code=405)


@router.delete("/mcp")
async def mcp_delete(request: Request):
    if not _authorized(request):
        return _unauthorized(request)
    return Response(status_code=405)
