from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..db import SessionLocal
from ..models import AuditLog, Booking, Document, KV, PaymentAccount, Rule, Supplier, Tenant, now
from ..services import actions, audit, bexio as bx, pain001, payments, pipeline, rules
from ..services.zammad import ZammadClient

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
ZURICH = ZoneInfo("Europe/Zurich")


def _as_date(value):
    """datetime (aware ones in Zurich time), date or ISO string -> date/datetime; None if empty, the value itself if unparsable."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip()) if len(value.strip()) > 10 else date.fromisoformat(value.strip())
        except ValueError:
            return value
    if isinstance(value, datetime) and value.tzinfo:
        value = value.astimezone(ZURICH)
    return value


def chdate(value) -> str:
    """Swiss date: 27.10.2026."""
    v = _as_date(value)
    if v is None:
        return ""
    return v.strftime("%d.%m.%Y") if isinstance(v, date) else v


def chdatetime(value, seconds: bool = False) -> str:
    """Swiss date and time: 27.10.2026 14:05 (with seconds on request); a plain date shows without time."""
    v = _as_date(value)
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")
    return v.strftime("%d.%m.%Y") if isinstance(v, date) else v


templates.env.filters["chdate"] = chdate
templates.env.filters["chdatetime"] = chdatetime


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


def _payment_labels(db: Session) -> dict[int, str]:
    return {p.id: p.label + (f" ****{p.card_last4}" if p.card_last4 else "") for p in db.query(PaymentAccount).all()}


def _chf_docs(db: Session, tenant: str = ""):
    """Paid foreign-currency documents waiting for the CHF amount."""
    q = db.query(Document).join(Tenant).filter(Document.state == "needs_chf")
    if tenant:
        q = q.filter(Tenant.code == tenant)
    return q.order_by(Document.received_at.asc())


def _ctx(request: Request, db: Session, sess: dict | None, **kw) -> dict:
    return {"request": request, "sess": sess, "tenants": _tenants(db), "counts": _counts(db), "chf_count": _chf_docs(db).count(),
            "settings": get_settings(), **kw}


def _tenant_or_404(db: Session, code: str) -> Tenant:
    t = db.query(Tenant).filter(Tenant.code == code).first()
    if not t:
        raise HTTPException(404)
    return t



def _resolve_account(tenant: Tenant, value) -> int | None:
    """Form value of an account picker: account number (preferred), 'no name', or a bexio id."""
    v = (str(value or "")).strip()
    if not v:
        return None
    first = v.split()[0]
    accounts = (tenant.master_data or {}).get("accounts", [])
    for a in accounts:
        if a.get("no") == first:
            return a["id"]
    for a in accounts:
        if str(a.get("id")) == first:
            return a["id"]
    return None


def _resolve_tax(tenant: Tenant, value) -> int | None:
    v = (str(value or "")).strip()
    if not v:
        return None
    first = v.split()[0]
    for x in (tenant.master_data or {}).get("taxes", []):
        if x.get("code") == first or str(x.get("id")) == first:
            return x["id"]
    return None


# --- auth --------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None, "settings": get_settings()})


def _secure() -> bool:
    return get_settings().public_base_url.startswith("https")


def _lock_message(seconds: int) -> str:
    if seconds <= 0:
        return "Zu viele Fehlversuche. Bitte später erneut versuchen."
    return f"Zu viele Fehlversuche. Anmeldung für {seconds} Sekunden gesperrt."


def _finish_login() -> RedirectResponse:
    resp = RedirectResponse("/todo", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.create_session(), max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=_secure())
    resp.delete_cookie(auth.PRE_COOKIE)
    return resp


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "?"
    if auth.blocked(ip):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": _lock_message(auth.locked_for()), "settings": get_settings()}, status_code=429, headers={"Retry-After": str(max(1, auth.locked_for()))})
    if not auth.check_password(username, password, ip):
        wait = auth.locked_for()
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": _lock_message(wait) if wait else "Anmeldung fehlgeschlagen", "settings": get_settings()}, status_code=429 if wait else 401)
    if not auth.totp_required():
        return _finish_login()
    # second step: one-time code
    resp = RedirectResponse("/login/totp", status_code=303)
    resp.set_cookie(auth.PRE_COOKIE, auth.create_pre_session(), max_age=auth.PRE_MAX_AGE, httponly=True, samesite="lax", secure=_secure())
    return resp


@router.get("/login/totp", response_class=HTMLResponse)
def login_totp_form(request: Request):
    if not auth.read_pre_session(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login_totp.html", {"request": request, "error": None})


@router.post("/login/totp")
def login_totp(request: Request, totp: str = Form("")):
    ip = request.client.host if request.client else "?"
    if not auth.read_pre_session(request):
        return RedirectResponse("/login", status_code=303)
    if auth.blocked(ip):
        return templates.TemplateResponse(request, "login_totp.html", {"request": request, "error": _lock_message(auth.locked_for())}, status_code=429, headers={"Retry-After": str(max(1, auth.locked_for()))})
    if not auth.check_totp(totp, ip):
        wait = auth.locked_for()
        return templates.TemplateResponse(request, "login_totp.html", {"request": request, "error": _lock_message(wait) if wait else "Code ungültig"}, status_code=429 if wait else 401)
    return _finish_login()


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
def todo(request: Request, tenant: str = "", state: str = "", err: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    q = db.query(Document).join(Tenant)
    if tenant:
        q = q.filter(Tenant.code == tenant)
    states = [state] if state else ["needs_review", "error"]
    docs = q.filter(Document.state.in_(states)).order_by(Document.received_at.asc()).limit(200).all()
    chf_docs = [] if state else _chf_docs(db, tenant).limit(200).all()
    return templates.TemplateResponse(request, "todo.html", _ctx(request, db, sess, docs=docs, chf_docs=chf_docs, tenant=tenant, state=state, err=err,
                                                                    payment_labels=_payment_labels(db)))


@router.get("/chf", response_class=HTMLResponse)
def chf_page(request: Request, tenant: str = "", err: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    return templates.TemplateResponse(request, "chf.html", _ctx(request, db, sess, chf_docs=_chf_docs(db, tenant).limit(500).all(), tenant=tenant, err=err,
                                                                   payment_labels=_payment_labels(db)))


@router.post("/docs/{doc_id}/chf")
async def doc_chf(request: Request, doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    form = await request.form()
    back = form.get("back") if form.get("back") in ("/todo", "/chf") else "/chf"
    back += f"?tenant={form.get('tenant') or ''}"
    try:
        amount_chf = round(float(str(form.get("amount_chf") or "").replace("'", "").replace(",", ".")), 2)
    except ValueError:
        amount_chf = 0
    if amount_chf <= 0:
        return RedirectResponse(back + "&err=" + quote(f"Beleg {doc.id}: CHF-Betrag fehlt"), status_code=303)
    if doc.state != "needs_chf":
        return RedirectResponse(back + "&err=" + quote(f"Beleg {doc.id} ist im Status {doc.state}"), status_code=303)
    z = _zammad()
    try:
        res = actions.book_chf(db, z, doc, amount_chf, actor=sess["u"])
    finally:
        if z:
            z.close()
    if not res["booked"]:
        return RedirectResponse(f"/docs/{doc.id}", status_code=303)
    return RedirectResponse(back, status_code=303)


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
    dec["account_id"] = _resolve_account(doc.tenant, form.get("account_no")) if "account_no" in form else integer(form.get("account_id"))
    dec["tax_id"] = _resolve_tax(doc.tenant, form.get("tax_code")) if "tax_code" in form else integer(form.get("tax_id"))
    dec["payment_mode"] = form.get("payment_mode") or "open"
    dec["payment_account_id"] = integer(form.get("payment_account_id")) if dec["payment_mode"] not in ("open", "credit") else None
    dec["booking_text"] = (form.get("booking_text") or "").strip()[:200]
    dec["amount"] = num(form.get("amount"))
    dec["currency"] = (form.get("currency") or "CHF").upper()[:3]
    dec["doc_date"] = (form.get("doc_date") or "").strip()
    dec["due_date"] = (form.get("due_date") or "").strip()
    dec["document_number"] = (form.get("document_number") or "").strip()[:80]
    dec["already_paid"] = form.get("already_paid") == "1"
    if "amount_chf" in form:
        dec["amount_chf"] = num(form.get("amount_chf"))
    if "line_amount" in form:
        # positions (one per VAT rate); rows without amount are dropped, fewer than two means a single line
        lines = []
        for title, amount, acc, tax in zip(form.getlist("line_title"), form.getlist("line_amount"),
                                           form.getlist("line_account_no"), form.getlist("line_tax_code")):
            if num(amount):
                lines.append({"title": (title or "").strip()[:80], "amount": num(amount),
                              "account_id": _resolve_account(doc.tenant, acc), "tax_id": _resolve_tax(doc.tenant, tax)})
        dec["lines"] = rules.with_net(doc.tenant, lines) if len(lines) > 1 else []
    # supplier rename / merge
    if dec["supplier_name"] and (not doc.supplier or rules.normalize_name(dec["supplier_name"]) != doc.supplier.name_key):
        # also name variants: a merged supplier keeps the other spelling as variant (sipcall: no new supplier on booking)
        existing = rules.find_supplier_by_name(db, doc.tenant_id, dec["supplier_name"])
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
    if dec.get("payment_mode") not in ("open", "credit") and not dec.get("payment_account_id"):
        missing.append("payment_account_id")
    if actions.needs_chf(dec) and not dec.get("amount_chf"):
        missing.append("Betrag CHF")
    if dec.get("lines") and dec.get("amount") and abs(sum(ln["amount"] for ln in dec["lines"]) - dec["amount"]) > 0.005:
        missing.append(f"Positionen ergeben {sum(ln['amount'] for ln in dec['lines']):.2f} statt {dec['amount']:.2f}")
    if missing or not doc.supplier:
        doc.state = "needs_review"
        doc.review_reason = "Felder fehlen: " + ", ".join(missing or ["Lieferant"])
        db.commit()
        return RedirectResponse(f"/docs/{doc.id}", status_code=303)
    remember = form.get("remember") == "1"
    keywords = [k.strip() for k in (form.get("keywords") or "").split(",") if k.strip()]
    z = _zammad()
    try:
        res = actions.book_with_decision(db, z, doc, dec, remember=remember, keywords=keywords, actor=sess["u"])
    finally:
        if z:
            z.close()
    if not res["booked"]:
        return RedirectResponse(f"/docs/{doc.id}", status_code=303)
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
    z = _zammad()
    try:
        actions.skip_document(db, z, doc, kind, close_ticket=form.get("close_ticket") == "1", actor=sess["u"])
    finally:
        if z:
            z.close()
    return RedirectResponse(f"/todo?tenant={doc.tenant.code}", status_code=303)


@router.post("/docs/{doc_id}/reset")
def doc_reset(doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    client = bx.BexioClient(db, doc.tenant)
    try:
        actions.reset_booking(db, client, doc, actor=sess["u"])
    except ValueError as e:
        doc.last_error = str(e)
        db.commit()
    finally:
        client.close()
    return RedirectResponse(f"/docs/{doc.id}", status_code=303)


@router.post("/docs/{doc_id}/restore")
def doc_restore(doc_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404)
    actions.restore_document(db, doc, actor=sess["u"])
    return RedirectResponse(f"/docs/{doc.id}", status_code=303)


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


@router.post("/bookings/{booking_id}/restore")
def booking_restore(booking_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404)
    actions.restore_document(db, b.document, actor=sess["u"])
    return RedirectResponse("/bookings", status_code=303)


# --- payments (pain.001) -----------------------------------------------------------
@router.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, tenant: str = "", msg: str = "", err: str = "", dl: str = "", db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    tenants = _tenants(db)
    t = next((x for x in tenants if x.code == tenant), None) or (tenants[0] if tenants else None)
    if not t:
        raise HTTPException(404)
    rows = payments.open_bills(db, t)
    in_file = [r for r in payments.open_bills(db, t, include_in_file=True) if r["payment_file"]]
    files = payments.list_files(t)
    # just created: the page starts the download (only for an existing, not withdrawn file of this tenant)
    download = next((f["name"] for f in files if f["name"] == dl and not f["void"]), None)
    return templates.TemplateResponse(request, "payments.html", _ctx(request, db, sess, tenant=t, rows=rows, in_file=in_file, files=files, download=download,
                                                                      today=pain001.next_bank_day(payments.local_today()).isoformat(), msg=msg, err=err))


@router.post("/payments/file")
async def payments_file(request: Request, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    f = await request.form()
    t = _tenant_or_404(db, f.get("tenant", ""))
    ids = [int(x) for x in f.getlist("booking_id") if str(x).isdigit()]
    try:
        exec_date = datetime.strptime(f.get("execution_date", ""), "%Y-%m-%d").date() if f.get("execution_date") else None
    except ValueError:
        exec_date = None
    z = _zammad()
    try:
        name, data, rows = payments.create_file(db, z, t, ids, exec_date, actor=sess["u"])
    except ValueError as e:
        return RedirectResponse(f"/payments?tenant={t.code}&err={e}", status_code=303)
    finally:
        if z:
            z.close()
    msg = quote(f"Zahlfile {name} erstellt ({len(rows)} Zahlungen), der Download startet automatisch")
    return RedirectResponse(f"/payments?tenant={t.code}&msg={msg}&dl={quote(name)}", status_code=303)


@router.get("/payments/files/{code}/{name}")
def payments_download(code: str, name: str, db: Session = Depends(get_db), sess: dict = Depends(auth.require_login)):
    t = _tenant_or_404(db, code)
    path = payments.file_path(t, name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="application/xml", filename=name)


def _replace(db: Session, booking_id: int, sess: dict, remove: bool) -> RedirectResponse:
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404)
    code = db.get(Tenant, b.tenant_id).code
    z = _zammad()
    try:
        res = payments.replace_file(db, z, b, actor=sess["u"], remove=remove)
    except ValueError as e:
        return RedirectResponse(f"/payments?tenant={code}&err={e}", status_code=303)
    finally:
        if z:
            z.close()
    if res["new"]:
        msg = f"{res['old']} gesperrt, neues Zahlfile {res['new']} mit {len(res['kept'])} Zahlungen, der Download startet automatisch"
        return RedirectResponse(f"/payments?tenant={code}&msg={quote(msg)}&dl={quote(res['new'])}", status_code=303)
    msg = f"{res['old']} gesperrt, keine Zahlung mehr übrig"
    return RedirectResponse(f"/payments?tenant={code}&msg={quote(msg)}", status_code=303)


@router.post("/payments/replace/{booking_id}")
def payments_replace(booking_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    return _replace(db, booking_id, sess, remove=False)


@router.post("/payments/remove/{booking_id}")
def payments_remove(booking_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    return _replace(db, booking_id, sess, remove=True)


@router.post("/payments/unmark/{booking_id}")
def payments_unmark(booking_id: int, db: Session = Depends(get_db), sess: dict = Depends(auth.require_csrf)):
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404)
    code = db.get(Tenant, b.tenant_id).code
    z = _zammad()
    try:
        payments.unmark(db, z, b, actor=sess["u"])  # releases every payment of that file
    except ValueError as e:
        return RedirectResponse(f"/payments?tenant={code}&err={e}", status_code=303)
    finally:
        if z:
            z.close()
    return RedirectResponse(f"/payments?tenant={code}&msg=Zahlfile zurückgezogen und gesperrt, Rechnungen wieder offen", status_code=303)


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

    t = db.get(Tenant, r.tenant_id)
    r.account_id = _resolve_account(t, f.get("account_no"))
    r.tax_id = _resolve_tax(t, f.get("tax_code"))
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
    sup.street = (f.get("street") or "").strip()[:100] or None
    sup.postcode = (f.get("postcode") or "").strip()[:16] or None
    sup.city = (f.get("city") or "").strip()[:60] or None
    sup.country = (f.get("country") or "").strip().upper()[:2] or None
    audit.log(db, "rule.saved", tenant_id=r.tenant_id, actor=sess["u"], entity="rule", entity_id=r.id)
    db.commit()
    return RedirectResponse(f"/rules?tenant={t.code}", status_code=303)


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
    pa.bexio_account_id = _resolve_account(t, f.get("bexio_account_no"))
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
    t.creditor_account_id = _resolve_account(t, f.get("creditor_account_no"))
    t.default_expense_account_id = _resolve_account(t, f.get("default_expense_account_no"))
    t.default_tax_id = _resolve_tax(t, f.get("default_tax_code"))
    t.fx_diff_account_id = _resolve_account(t, f.get("fx_diff_account_no"))
    t.fee_account_id = _resolve_account(t, f.get("fee_account_no"))
    if "commission_account_no" in f:
        t.commission_account_id = _resolve_account(t, f.get("commission_account_no"))
    t.bank_account_id = integer(f.get("bank_account_id"))
    t.payment_iban = rules.normalize_iban(f.get("payment_iban")) or None
    t.payment_name = (f.get("payment_name") or "").strip()[:70] or None
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
        res = pipeline.learn_from_history(db, t)
        audit.log(db, "rules.learned_from_history", tenant_id=t.id, actor=sess["u"], details=res)
        db.commit()
        msg = f"{code}: {res['created']} Regeln angelegt, {res['updated']} Zahlungsart aktualisiert"
        if res["unmapped_bank_accounts"]:
            msg += "; bexio-Bankkonten ohne Zahlkonto: " + ", ".join(f"{k} ({v})" for k, v in res["unmapped_bank_accounts"].items())
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
        company = bx.fetch_company(tokens["access_token"])
        problem = bx.company_problem(t, company)
        if problem:
            audit.log(db, "bexio.rejected", tenant_id=t.id, actor=sess["u"],
                      details={"company_id": company[0], "company": company[1]})
            db.commit()
            return RedirectResponse(f"/settings?msg={t.code}: {problem} Nichts gespeichert, bisherige Verbindung unverändert.", status_code=303)
        bx.store_tokens(db, t, tokens)
        t.bexio_company_id = t.bexio_company_id or company[0]
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
