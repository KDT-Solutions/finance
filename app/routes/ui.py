from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..db import SessionLocal
from ..models import AuditLog, Booking, Document, KV, PaymentAccount, Rule, Supplier, Tenant, now
from ..services import audit, bexio as bx, pipeline, rules
from ..services.zammad import ZammadClient

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _zammad() -> ZammadClient | None:
    s = get_settings()
    return ZammadClient() if (s.zammad_url and s.zammad_token) else None


def _tenants(db: Session) -> list[Tenant]:
    return db.query(Tenant).filter(Tenant.active.is_(True)).order_by(Tenant.id).all()


def _counts(db: Session) -> dict[str, int]:
    rows = db.query(Tenant.code, func.count(Document.id)).join(Document, Document.tenant_id == Tenant.id).filter(
        Document.state.in_(["needs_review", "error"])).group_by(Tenant.code).all()
    return {c: n for c, n in rows}


def _ctx(request: Request, db: Session, sess: dict | None, **kw) -> dict:
    return {"request": request, "sess": sess, "tenants": _tenants(db), "counts": _counts(db), "settings": get_settings(), **kw}


def _tenant_or_404(db: Session, code: str) -> Tenant:
    t = db.query(Tenant).filter(Tenant.code == code).first()
    if not t:
        raise HTTPException(404)
    return t


# --- auth --------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None, "settings": get_settings()})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), totp: str = Form("")):
    ip = request.client.host if request.client else "?"
    if not auth.check_login(username, password, totp, ip):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Anmeldung fehlgeschlagen", "settings": get_settings()}, status_code=401)
    resp = RedirectResponse("/todo", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.create_session(), max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
                    secure=get_settings().public_base_url.startswith("https"))
    return resp


@router.post("/logout")
def logout(sess: dict = Depends(auth.require_csrf)):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@router.get("/")
def index(sess: dict = Depends(auth.require_login)):
    return RedirectResponse("/todo", status_code=303)


# --- todo / documents --------------------------------------------------------------
@router.get("/todo", response_class=HTMLResponse)
def todo(request: Request, tenant: str = "", state: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    q = db.query(Document).join(Tenant)
    if tenant:
        q = q.filter(Tenant.code == tenant)
    states = [state] if state else ["needs_review", "error"]
    docs = q.filter(Document.state.in_(states)).order_by(Document.received_at.asc()).limit(200).all()
    return templates.TemplateResponse(request, "todo.html", _ctx(request, db, sess, docs=docs, tenant=tenant, state=state))


@router.get("/docs/{doc_id}", response_class=HTMLResponse)
def doc_detail(request: Request, doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    t = doc.tenant
    md = t.master_data or {}
    accounts = db.query(PaymentAccount).filter(PaymentAccount.tenant_id == t.id, PaymentAccount.active.is_(True)).all()
    nxt = db.query(Document).filter(Document.tenant_id == t.id, Document.state.in_(["needs_review", "error"]), Document.id != doc.id).order_by(Document.received_at.asc()).first()
    return templates.TemplateResponse(request, "doc.html", _ctx(request, db, sess, doc=doc, md=md, payment_accounts=accounts, next_doc=nxt,
                                                                   extraction=doc.extraction, dec=doc.decision or {}))


@router.get("/docs/{doc_id}/pdf")
def doc_pdf(doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    return FileResponse(doc.storage_path, media_type="application/pdf", filename=doc.filename, content_disposition_type="inline")


def _apply_form(db: Session, doc: Document, form) -> dict:
    dec = dict(doc.decision or {})

    def num(v):
        try:
            return round(float(str(v).replace("'", "").replace(",", ".")), 2)
        except (TypeError, ValueError):
            return None

    def integer(v):
        try:
            return int(v) if str(v).strip() else None
        except (TypeError, ValueError):
            return None

    dec["supplier_name"] = (form.get("supplier_name") or dec.get("supplier_name") or "").strip()
    dec["account_id"] = integer(form.get("account_id"))
    dec["tax_id"] = integer(form.get("tax_id"))
    dec["payment_mode"] = form.get("payment_mode") or "open"
    dec["payment_account_id"] = integer(form.get("payment_account_id")) if dec["payment_mode"] != "open" else None
    dec["booking_text"] = (form.get("booking_text") or "").strip()[:200]
    dec["amount"] = num(form.get("amount"))
    dec["currency"] = (form.get("currency") or "CHF").upper()[:3]
    dec["doc_date"] = (form.get("doc_date") or "").strip()
    dec["due_date"] = (form.get("due_date") or "").strip()
    dec["document_number"] = (form.get("document_number") or "").strip()[:80]
    # supplier rename / merge
    if dec["supplier_name"] and (not doc.supplier or rules.normalize_name(dec["supplier_name"]) != doc.supplier.name_key):
        existing = db.query(Supplier).filter(Supplier.tenant_id == doc.tenant_id, Supplier.name_key == rules.normalize_name(dec["supplier_name"])).first()
        if existing:
            doc.supplier = existing
        elif doc.supplier and not doc.supplier.rules and db.query(Document).filter(Document.supplier_id == doc.supplier.id).count() <= 1:
            doc.supplier.display_name = dec["supplier_name"][:200]
            doc.supplier.name_key = rules.normalize_name(dec["supplier_name"])
        else:
            sup = Supplier(tenant_id=doc.tenant_id, display_name=dec["supplier_name"][:200], name_key=rules.normalize_name(dec["supplier_name"]), ibans=[], name_variants=[])
            db.add(sup)
            db.flush()
            doc.supplier = sup
    doc.decision = dec
    return dec


@router.post("/docs/{doc_id}/book")
async def doc_book(request: Request, doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    form = await request.form()
    dec = _apply_form(db, doc, form)
    missing = [k for k in ("account_id", "amount", "doc_date") if not dec.get(k)]
    if dec.get("payment_mode") != "open" and not dec.get("payment_account_id"):
        missing.append("payment_account_id")
    if missing or not doc.supplier:
        doc.state = "needs_review"
        doc.review_reason = "Felder fehlen: " + ", ".join(missing or ["Lieferant"])
        db.commit()
        return RedirectResponse(f"/docs/{doc.id}", status_code=303)
    remember = form.get("remember") == "1"
    keywords = [k.strip() for k in (form.get("keywords") or "").split(",") if k.strip()]
    audit.log(db, "document.decided", tenant_id=doc.tenant_id, actor=sess["u"], entity="document", entity_id=doc.id, details=dec)
    z = _zammad()
    try:
        # move to toPay if it is an open invoice still sitting in the Finance group
        if dec.get("payment_mode") == "open" and z and doc.zammad_ticket_id and doc.zammad_group == doc.tenant.zammad_group_paid:
            try:
                z.set_group(doc.zammad_ticket_id, doc.tenant.zammad_group_topay)
                doc.zammad_group = doc.tenant.zammad_group_topay
            except Exception:  # noqa: BLE001
                log.exception("move failed")
        pipeline.book(db, z, doc, actor=sess["u"])
        if remember and doc.supplier:
            rule = rules.learn_from_decision(db, doc.tenant, doc.supplier, doc.rule if (doc.rule and (doc.rule.keywords or []) == keywords) else None, dec, keywords=keywords, source="review")
            doc.rule = rule
            audit.log(db, "rule.learned", tenant_id=doc.tenant_id, actor=sess["u"], entity="rule", entity_id=rule.id, details={"supplier": doc.supplier.display_name, "auto_book": rule.auto_book})
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("booking failed: %s", e)
        return RedirectResponse(f"/docs/{doc.id}", status_code=303)
    finally:
        if z:
            z.close()
    nxt = db.query(Document).filter(Document.tenant_id == doc.tenant_id, Document.state.in_(["needs_review", "error"])).order_by(Document.received_at.asc()).first()
    return RedirectResponse(f"/docs/{nxt.id}" if nxt else f"/todo?tenant={doc.tenant.code}", status_code=303)


@router.post("/docs/{doc_id}/save")
async def doc_save(request: Request, doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    _apply_form(db, doc, await request.form())
    db.commit()
    return RedirectResponse(f"/docs/{doc.id}", status_code=303)


@router.post("/docs/{doc_id}/skip")
async def doc_skip(request: Request, doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    form = await request.form()
    kind = form.get("kind", "skip")
    doc.state = {"skip": "skipped", "not_receipt": "not_a_receipt", "cancel": "cancelled"}.get(kind, "skipped")
    audit.log(db, f"document.{doc.state}", tenant_id=doc.tenant_id, actor=sess["u"], entity="document", entity_id=doc.id)
    db.commit()
    if kind == "not_receipt" and form.get("close_ticket") == "1" and doc.zammad_ticket_id:
        z = _zammad()
        if z:
            try:
                z.add_note(doc.zammad_ticket_id, "finance: Kein Beleg, Ticket geschlossen.")
                z.set_state(doc.zammad_ticket_id, "closed")
            except Exception:  # noqa: BLE001
                log.exception("close failed")
            finally:
                z.close()
    return RedirectResponse(f"/todo?tenant={doc.tenant.code}", status_code=303)


@router.post("/docs/{doc_id}/reprocess")
def doc_reprocess(doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    z = _zammad()
    try:
        pipeline.process_document(db, z, doc)
    finally:
        if z:
            z.close()
    return RedirectResponse(f"/docs/{doc.id}", status_code=303)


@router.post("/upload")
async def upload(request: Request, tenant: str = Form(...), file: UploadFile = None, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    t = _tenant_or_404(db, tenant)
    content = await file.read() if file else b""
    if not content.startswith(b"%PDF") or len(content) > get_settings().max_pdf_bytes:
        raise HTTPException(400, "Nur PDF bis 20 MB")
    sha = hashlib.sha256(content).hexdigest()
    doc = db.query(Document).filter(Document.tenant_id == t.id, Document.sha256 == sha).first()
    if not doc:
        path = pipeline.store_pdf(t, sha, content)
        doc = Document(tenant_id=t.id, sha256=sha, filename=file.filename or "upload.pdf", storage_path=path, size=len(content), source="upload", zammad_group=t.zammad_group_paid)
        db.add(doc)
        db.commit()
        audit.log(db, "document.uploaded", tenant_id=t.id, actor=sess["u"], entity="document", entity_id=doc.id)
        db.commit()
        pipeline.process_document(db, None, doc)
    return RedirectResponse(f"/docs/{doc.id}", status_code=303)


# --- bookings / rules / accounts / audit ---------------------------------------------
@router.get("/bookings", response_class=HTMLResponse)
def bookings(request: Request, tenant: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    q = db.query(Booking).join(Tenant)
    if tenant:
        q = q.filter(Tenant.code == tenant)
    rows = q.order_by(Booking.booked_at.desc()).limit(300).all()
    return templates.TemplateResponse(request, "bookings.html", _ctx(request, db, sess, rows=rows, tenant=tenant))


@router.post("/bookings/{booking_id}/cancel")
def booking_cancel(booking_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404)
    b.status = "cancelled"
    b.document.state = "cancelled"
    audit.log(db, "booking.cancelled", tenant_id=b.tenant_id, actor=sess["u"], entity="booking", entity_id=b.id)
    db.commit()
    return RedirectResponse("/bookings", status_code=303)


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request, tenant: str = "", q: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    query = db.query(Rule).join(Supplier).join(Tenant, Rule.tenant_id == Tenant.id)
    if tenant:
        query = query.filter(Tenant.code == tenant)
    if q:
        query = query.filter(Supplier.display_name.ilike(f"%{q}%"))
    rows = query.order_by(Supplier.display_name).limit(500).all()
    return templates.TemplateResponse(request, "rules.html", _ctx(request, db, sess, rows=rows, tenant=tenant, q=q))


@router.post("/rules/new")
async def rule_new(request: Request, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    f = await request.form()
    t = _tenant_or_404(db, f.get("tenant", ""))
    name = (f.get("supplier_name") or "").strip()
    if not name:
        raise HTTPException(400, "Lieferant fehlt")
    sup = db.query(Supplier).filter(Supplier.tenant_id == t.id, Supplier.name_key == rules.normalize_name(name)).first()
    if not sup:
        sup = Supplier(tenant_id=t.id, display_name=name[:200], name_key=rules.normalize_name(name), ibans=[], name_variants=[])
        db.add(sup)
        db.flush()
    r = Rule(tenant_id=t.id, supplier_id=sup.id, keywords=[], learned_from="manual", payment_mode="open")
    db.add(r)
    db.commit()
    return RedirectResponse(f"/rules/{r.id}", status_code=303)


@router.get("/rules/{rule_id}", response_class=HTMLResponse)
def rule_edit(request: Request, rule_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    r = db.get(Rule, rule_id)
    if not r:
        raise HTTPException(404)
    t = db.get(Tenant, r.tenant_id)
    accounts = db.query(PaymentAccount).filter(PaymentAccount.tenant_id == t.id).all()
    return templates.TemplateResponse(request, "rule.html", _ctx(request, db, sess, rule=r, t=t, md=t.master_data or {}, payment_accounts=accounts))


@router.post("/rules/{rule_id}")
async def rule_save(request: Request, rule_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    r = db.get(Rule, rule_id)
    if not r:
        raise HTTPException(404)
    f = await request.form()

    def integer(v):
        try:
            return int(v) if str(v).strip() else None
        except (TypeError, ValueError):
            return None

    def num(v):
        try:
            return float(v) if str(v).strip() else None
        except (TypeError, ValueError):
            return None

    r.account_id = integer(f.get("account_id"))
    r.tax_id = integer(f.get("tax_id"))
    r.booking_text = (f.get("booking_text") or "")[:200]
    r.payment_mode = f.get("payment_mode") or "open"
    r.payment_account_id = integer(f.get("payment_account_id")) if r.payment_mode != "open" else None
    r.keywords = [k.strip() for k in (f.get("keywords") or "").split(",") if k.strip()]
    r.amount_min = num(f.get("amount_min"))
    r.amount_max = num(f.get("amount_max"))
    r.auto_book = f.get("auto_book") == "1"
    r.active = f.get("active") == "1"
    r.learned_from = "manual"
    sup = r.supplier
    sup.ibans = [rules.normalize_iban(x) for x in (f.get("ibans") or "").split(",") if x.strip()]
    sup.uid = rules.normalize_uid(f.get("uid")) or None
    sup.name_variants = [x.strip() for x in (f.get("name_variants") or "").split(",") if x.strip()]
    sup.bexio_contact_id = integer(f.get("bexio_contact_id"))
    audit.log(db, "rule.saved", tenant_id=r.tenant_id, actor=sess["u"], entity="rule", entity_id=r.id)
    db.commit()
    return RedirectResponse(f"/rules?tenant={db.get(Tenant, r.tenant_id).code}", status_code=303)


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    rows = db.query(PaymentAccount).join(Tenant).order_by(Tenant.id, PaymentAccount.label).all()
    return templates.TemplateResponse(request, "accounts.html", _ctx(request, db, sess, rows=rows))


@router.post("/accounts")
async def account_save(request: Request, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    f = await request.form()
    t = _tenant_or_404(db, f.get("tenant", ""))
    pid = f.get("id")
    pa = db.get(PaymentAccount, int(pid)) if pid else PaymentAccount(tenant_id=t.id)
    pa.label = (f.get("label") or "")[:80]
    pa.kind = f.get("kind") or "card"
    pa.card_last4 = (f.get("card_last4") or "")[-4:] or None
    try:
        pa.bexio_account_id = int(f.get("bexio_account_id")) if f.get("bexio_account_id") else None
    except ValueError:
        pa.bexio_account_id = None
    pa.bexio_account_no = pipeline._master(t, "accounts", pa.bexio_account_id).get("no")
    pa.active = f.get("active", "1") == "1"
    db.add(pa)
    audit.log(db, "payment_account.saved", tenant_id=t.id, actor=sess["u"], entity="payment_account", details={"label": pa.label})
    db.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
def audit_list(request: Request, tenant: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    q = db.query(AuditLog)
    if tenant:
        t = _tenant_or_404(db, tenant)
        q = q.filter(AuditLog.tenant_id == t.id)
    rows = q.order_by(AuditLog.at.desc()).limit(300).all()
    return templates.TemplateResponse(request, "audit.html", _ctx(request, db, sess, rows=rows, tenant=tenant))


# --- settings / bexio ------------------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    zgroups: list[str] = []
    z = _zammad()
    if z:
        try:
            zgroups = [g["name"] for g in z.groups() if g.get("active", True)]
        except Exception as e:  # noqa: BLE001
            zgroups = [f"Zammad-Fehler: {e}"]
        finally:
            z.close()
    return templates.TemplateResponse(request, "settings.html", _ctx(request, db, sess, zgroups=zgroups, msg=msg, redirect_uri=bx.redirect_uri()))


@router.post("/settings/tenant/{code}")
async def tenant_save(request: Request, code: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    t = _tenant_or_404(db, code)
    f = await request.form()

    def integer(v):
        try:
            return int(v) if str(v).strip() else None
        except (TypeError, ValueError):
            return None

    t.zammad_group_paid = (f.get("zammad_group_paid") or t.zammad_group_paid)[:80]
    t.zammad_group_topay = (f.get("zammad_group_topay") or t.zammad_group_topay)[:80]
    t.creditor_account_id = integer(f.get("creditor_account_id"))
    t.default_expense_account_id = integer(f.get("default_expense_account_id"))
    t.default_tax_id = integer(f.get("default_tax_id"))
    t.fx_diff_account_id = integer(f.get("fx_diff_account_id"))
    t.fee_account_id = integer(f.get("fee_account_id"))
    t.active = f.get("active", "1") == "1"
    audit.log(db, "tenant.saved", tenant_id=t.id, actor=sess["u"], entity="tenant", entity_id=t.id)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/tenant/{code}/refresh")
def tenant_refresh(code: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    t = _tenant_or_404(db, code)
    client = bx.BexioClient(db, t)
    try:
        client.refresh_master_data()
        db.commit()
        msg = f"{code}: Stammdaten aktualisiert"
    except bx.BexioError as e:
        msg = f"{code}: {e}"
    finally:
        client.close()
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@router.post("/settings/tenant/{code}/learn")
def tenant_learn(code: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    t = _tenant_or_404(db, code)
    try:
        n = pipeline.learn_from_history(db, t)
        audit.log(db, "rules.learned_from_history", tenant_id=t.id, actor=sess["u"], details={"rules": n})
        db.commit()
        msg = f"{code}: {n} Regeln aus der bexio-Historie angelegt"
    except bx.BexioError as e:
        msg = f"{code}: {e}"
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@router.post("/settings/tenant/{code}/disconnect")
def tenant_disconnect(code: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    t = _tenant_or_404(db, code)
    bx.disconnect(db, t)
    audit.log(db, "bexio.disconnected", tenant_id=t.id, actor=sess["u"])
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/auth/bexio/start")
def bexio_start(tenant: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    t = _tenant_or_404(db, tenant)
    state = bx.new_state(t.code)
    db.merge(KV(key=f"oauth_state:{state}", value=t.code))
    db.commit()
    return RedirectResponse(bx.authorize_url(state), status_code=303)


@router.get("/auth/bexio/callback")
def bexio_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    if error:
        return RedirectResponse(f"/settings?msg=bexio: {error}", status_code=303)
    kv = db.get(KV, f"oauth_state:{state}")
    if not kv or (now() - kv.updated_at).total_seconds() > 900:
        raise HTTPException(400, "OAuth-State ungültig oder abgelaufen")
    t = _tenant_or_404(db, kv.value or "")
    db.delete(kv)
    try:
        tokens = bx.exchange_code(code)
        bx.store_tokens(db, t, tokens)
        t.bexio_connected_at = now()
        db.commit()
        client = bx.BexioClient(db, t)
        try:
            client.refresh_master_data()
        finally:
            client.close()
        db.commit()
        audit.log(db, "bexio.connected", tenant_id=t.id, actor=sess["u"], details={"company": t.bexio_company_name})
        db.commit()
        msg = f"{t.code}: bexio verbunden ({t.bexio_company_name})"
    except bx.BexioError as e:
        db.rollback()
        msg = f"{t.code}: {e}"
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)
