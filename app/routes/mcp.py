"""MCP endpoint (Streamable HTTP, JSON-RPC 2.0) so the tool can be driven from a chat client.

POST /mcp with "Authorization: Bearer <MCP_TOKEN>". Writing tools run only with confirm=true,
otherwise they return a preview. Nothing is deleted; rules and payment accounts are deactivated.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..db import SessionLocal
from ..models import AuditLog, Booking, Document, PaymentAccount, Rule, Supplier, Tenant
from ..services import actions, audit, bexio as bx, pipeline, rules
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
    sup = db.query(Supplier).filter(Supplier.tenant_id == t.id, Supplier.name_key == key).first()
    if not sup:
        for s in db.query(Supplier).filter(Supplier.tenant_id == t.id).all():
            if key in [rules.normalize_name(v) for v in (s.name_variants or [])]:
                return s
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
        "bexio_contact_id": r.supplier.bexio_contact_id,
    }


def _doc_dict(d: Document, full: bool = False) -> dict:
    dec = d.decision or {}
    out = {
        "id": d.id, "tenant": d.tenant.code, "state": d.state, "received_at": d.received_at.isoformat() if d.received_at else None,
        "filename": d.filename, "ticket": d.zammad_ticket_number, "ticket_title": d.ticket_title, "zammad_group": d.zammad_group,
        "supplier": d.supplier.display_name if d.supplier else dec.get("supplier_name"),
        "amount": dec.get("amount"), "currency": dec.get("currency"), "doc_date": dec.get("doc_date"), "due_date": dec.get("due_date"),
        "document_number": dec.get("document_number"), "payment_mode": dec.get("payment_mode"),
        "account_no": pipeline._master(d.tenant, "accounts", dec.get("account_id")).get("no"),
        "tax_code": pipeline._master(d.tenant, "taxes", dec.get("tax_id")).get("code"),
        "review_reason": d.review_reason, "last_error": d.last_error, "reasons": dec.get("reasons"),
        "url": f"{get_settings().public_base_url}/docs/{d.id}",
    }
    if full:
        ex = d.extraction
        out["extraction"] = {"qr": ex.qr if ex else None, "llm": ex.llm if ex else None, "model": ex.model if ex else None}
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
        n = db.query(Document).filter(Document.tenant_id == t.id, Document.state.in_(["needs_review", "error"])).count()
        out.append({"code": t.code, "name": t.name, "active": t.active, "bexio_connected": t.bexio_connected,
                    "bexio_company": t.bexio_company_name, "master_data_at": t.master_data_at.isoformat() if t.master_data_at else None,
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
        "default_tax_code": pipeline._master(t, "taxes", t.default_tax_id).get("code"),
        "bank_account": next(({"id": b["id"], "name": b["name"], "iban": b["iban"]} for b in md.get("bank_accounts", []) if b["id"] == t.bank_account_id), None),
        "bank_accounts": md.get("bank_accounts", []),
        "payment_accounts": [{"id": p.id, "label": p.label, "kind": p.kind, "card_last4": p.card_last4, "bexio_account_no": p.bexio_account_no, "active": p.active}
                             for p in db.query(PaymentAccount).filter(PaymentAccount.tenant_id == t.id).all()],
        "counts": {"accounts": len(md.get("accounts", [])), "taxes": len(md.get("taxes", []))},
    }


@tool("set_tenant_settings", "Einstellungen eines Mandanten setzen (Konten per Kontonummer, Steuer per Code, Bankkonto per bexio-ID).",
      {"tenant": TENANT, "default_expense_account_no": {"type": "string"}, "creditor_account_no": {"type": "string"},
       "fee_account_no": {"type": "string"}, "fx_diff_account_no": {"type": "string"}, "default_tax_code": {"type": "string"},
       "bank_account_id": {"type": "integer"}, "zammad_group_paid": {"type": "string"}, "zammad_group_topay": {"type": "string"},
       "active": {"type": "boolean"}, "confirm": CONFIRM}, ["tenant"])
def t_set_settings(db, a):
    t = _tenant(db, a["tenant"])
    changes = {}
    for key, col in (("default_expense_account_no", "default_expense_account_id"), ("creditor_account_no", "creditor_account_id"),
                     ("fee_account_no", "fee_account_id"), ("fx_diff_account_no", "fx_diff_account_id")):
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


@tool("learn_from_history", "Regeln aus bezahlten Kreditorenrechnungen in bexio ableiten (Konto je Lieferant, Betragsband).", {"tenant": TENANT, "confirm": CONFIRM}, ["tenant"])
def t_learn(db, a):
    t = _tenant(db, a["tenant"])
    if not t.bexio_connected:
        raise ToolError(f"{t.code}: bexio nicht verbunden")
    if not a.get("confirm"):
        return _preview(f"Regeln für {t.code} aus der bexio-Historie lernen", {})
    n = pipeline.learn_from_history(db, t)
    audit.log(db, "rules.learned_history", tenant_id=t.id, actor=ACTOR, details={"created": n})
    db.commit()
    return {"ok": True, "rules_created": n}


@tool("list_accounts", "Kontenplan eines Mandanten (aus den Stammdaten), optional gefiltert nach Nummer oder Name.", {"tenant": TENANT, "q": {"type": "string"}}, ["tenant"])
def t_accounts(db, a):
    t = _tenant(db, a["tenant"])
    q = (a.get("q") or "").lower()
    return [x for x in (t.master_data or {}).get("accounts", []) if not q or q in x.get("no", "") or q in x.get("name", "").lower()]


@tool("list_taxes", "Steuercodes eines Mandanten.", {"tenant": TENANT}, ["tenant"])
def t_taxes(db, a):
    t = _tenant(db, a["tenant"])
    return (t.master_data or {}).get("taxes", [])


@tool("list_todo", "Belege in Prüfung oder mit Fehler (Standard), oder nach Status gefiltert.",
      {"tenant": TENANT, "state": {"type": "string", "description": "new | needs_review | error | booked | skipped | not_a_receipt | cancelled | extracted"},
       "limit": {"type": "integer", "default": 50}})
def t_todo(db, a):
    q = db.query(Document).join(Tenant)
    if a.get("tenant"):
        q = q.filter(Tenant.code == a["tenant"].upper())
    q = q.filter(Document.state == a["state"]) if a.get("state") else q.filter(Document.state.in_(["needs_review", "error"]))
    return [_doc_dict(d) for d in q.order_by(Document.received_at.asc()).limit(int(a.get("limit") or 50)).all()]


@tool("get_document", "Beleg im Detail: Extraktion (QR, LLM), Entscheid, Gründe, Buchung.", {"id": {"type": "integer"}}, ["id"])
def t_get_doc(db, a):
    d = db.get(Document, int(a["id"]))
    if not d:
        raise ToolError("Beleg nicht gefunden")
    return _doc_dict(d, full=True)


@tool("book_document", "Beleg buchen: Felder überschreiben (Konto per Nummer, Steuer per Code, Zahlkonto per Label) und in bexio verbuchen. "
      "payment_mode open = offene Kreditorenrechnung, card/private/bank_paid = bezahlt (manuelle Buchung gegen Zahlkonto). "
      "remember=true merkt sich die Entscheidung als Regel für den Lieferanten.",
      {"id": {"type": "integer"}, "supplier_name": {"type": "string"}, "account_no": {"type": "string"}, "tax_code": {"type": "string"},
       "payment_mode": {"type": "string", "enum": ["open", "card", "private", "bank_paid"]}, "payment_account": {"type": "string"},
       "booking_text": {"type": "string"}, "amount": {"type": "number"}, "currency": {"type": "string"}, "doc_date": {"type": "string"},
       "due_date": {"type": "string"}, "document_number": {"type": "string"}, "remember": {"type": "boolean", "default": False},
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
    for k in ("booking_text", "amount", "currency", "doc_date", "due_date", "document_number"):
        if k in a and a[k] is not None:
            dec[k] = a[k]
    dec["payment_mode"] = dec.get("payment_mode") or "open"
    if dec["payment_mode"] == "open":
        dec["payment_account_id"] = None
    missing = [k for k in ("account_id", "amount", "doc_date") if not dec.get(k)]
    if dec["payment_mode"] != "open" and not dec.get("payment_account_id"):
        missing.append("payment_account")
    if not dec.get("supplier_name") and not d.supplier:
        missing.append("supplier_name")
    summary = {
        "document": d.id, "tenant": t.code, "supplier": dec.get("supplier_name") or (d.supplier.display_name if d.supplier else None),
        "amount": dec.get("amount"), "currency": dec.get("currency", "CHF"), "doc_date": dec.get("doc_date"), "due_date": dec.get("due_date"),
        "account_no": pipeline._master(t, "accounts", dec.get("account_id")).get("no"), "tax_code": pipeline._master(t, "taxes", dec.get("tax_id")).get("code"),
        "payment_mode": dec["payment_mode"], "payment_account_id": dec.get("payment_account_id"), "remember": bool(a.get("remember")),
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
    "payment_mode": {"type": "string", "enum": ["open", "card", "private", "bank_paid"]},
    "payment_account": {"type": "string", "description": "Label oder ID des Zahlkontos (bei card/private/bank_paid)"},
    "booking_text": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}, "description": "leer = Standardregel des Lieferanten"},
    "amount_min": {"type": "number"}, "amount_max": {"type": "number"}, "auto_book": {"type": "boolean"}, "active": {"type": "boolean"},
    "ibans": {"type": "array", "items": {"type": "string"}}, "uid": {"type": "string", "description": "CHE-Nummer"},
    "name_variants": {"type": "array", "items": {"type": "string"}}, "bexio_contact_id": {"type": "integer"},
}


def _upsert_rule(db: Session, t: Tenant, spec: dict) -> tuple[Rule, bool, dict]:
    sup = _supplier(db, t, spec.get("supplier_name") or "", create=True)
    keywords = [k.strip() for k in (spec.get("keywords") or []) if k.strip()]
    rule = next((r for r in sup.rules if (r.keywords or []) == keywords), None)
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
    if rule.payment_mode == "open":
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


# --- JSON-RPC transport ---------------------------------------------------------------
def _authorized(request: Request) -> bool:
    token = get_settings().mcp_token
    if not token:
        return False
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth_header[7:].strip(), token)


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
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
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
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return Response(status_code=405)


@router.delete("/mcp")
async def mcp_delete(request: Request):
    return Response(status_code=405 if _authorized(request) else 401)
