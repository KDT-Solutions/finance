"""Daily due check: open creditor bills (purchase bills, API 4.0) and open debtor invoices (kb_invoice, API 2.0)
that are overdue or fall due within the next days. Read-only; the worker pushes the result once a day."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..models import Booking, Tenant
from . import bexio as bx

log = logging.getLogger(__name__)

# kb_invoice status ids that still expect money: 8 open, 16 partially paid, 31 unpaid (overdue)
OPEN_INVOICE_STATUS = {8, 16, 31}
PAGE = 500


def _d(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _amount(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def creditor_items(c, today: date, limit_day: date, in_file: dict[str, str]) -> list[dict]:
    """Open purchase bills with a pending amount and due date <= limit_day."""
    out, page = [], 1
    while True:
        res = c.list_bills(status="TODO", due_date_end=limit_day.isoformat(), limit=PAGE, page=page)
        rows = res.get("data", []) if isinstance(res, dict) else (res or [])
        for b in rows:
            due = _d(b.get("due_date"))
            pending = _amount(b.get("pending_amount"))
            if not due or due > limit_day or pending <= 0:
                continue
            out.append({
                "kind": "kreditor", "id": b.get("id"), "document_no": b.get("document_no"), "ref": b.get("vendor_ref"),
                "party": b.get("vendor") or "", "date": b.get("bill_date"), "due_date": due.isoformat(),
                "days_overdue": (today - due).days, "open": pending, "currency": b.get("currency_code") or "CHF",
                "payment_file": in_file.get(str(b.get("id")), ""), "url": bx.bill_web_url(b.get("id")),
            })
        if len(rows) < PAGE:
            break
        page += 1
    return out


def debtor_items(c, today: date, limit_day: date) -> list[dict]:
    """Open debtor invoices (status open/partial/unpaid) with due date (is_valid_to) <= limit_day."""
    out, offset, names = [], 0, {}
    while True:
        rows = c.get("/2.0/kb_invoice", limit=PAGE, offset=offset) or []
        for inv in rows:
            if inv.get("kb_item_status_id") not in OPEN_INVOICE_STATUS:
                continue
            due = _d(inv.get("is_valid_to"))
            if not due or due > limit_day:
                continue
            remaining = inv.get("total_remaining_payments")
            open_amount = _amount(remaining if remaining is not None else inv.get("total"))
            if open_amount <= 0:
                continue
            cid = inv.get("contact_id")
            if cid and cid not in names:
                try:
                    ct = c.get(f"/2.0/contact/{cid}") or {}
                    names[cid] = " ".join(x for x in (ct.get("name_1"), ct.get("name_2")) if x).strip()
                except bx.BexioError:
                    names[cid] = ""
            out.append({
                "kind": "debitor", "id": inv.get("id"), "document_no": inv.get("document_nr"), "ref": inv.get("title") or "",
                "party": names.get(cid, "") or f"Kontakt {cid}", "date": inv.get("is_valid_from"), "due_date": due.isoformat(),
                "days_overdue": (today - due).days, "open": open_amount, "currency": inv.get("currency_code") or "CHF",
                "payment_file": "", "url": bx.invoice_web_url(inv.get("id")),
            })
        if len(rows) < PAGE:
            break
        offset += PAGE
    return out


def check_tenant(db: Session, t: Tenant, *, days: int, today: date | None = None) -> dict:
    today = today or date.today()
    limit_day = today + timedelta(days=days)
    in_file = {b.bexio_bill_id: b.payment_file for b in db.query(Booking).filter(Booking.tenant_id == t.id, Booking.payment_file.isnot(None),
                                                                                  Booking.bexio_bill_id.isnot(None))}
    res = {"tenant": t.code, "kreditoren": [], "debitoren": [], "errors": []}
    c = bx.BexioClient(db, t)
    try:
        for key, fn in (("kreditoren", lambda: creditor_items(c, today, limit_day, in_file)), ("debitoren", lambda: debtor_items(c, today, limit_day))):
            try:
                res[key] = sorted(fn(), key=lambda x: x["due_date"])
            except Exception as e:  # noqa: BLE001
                log.exception("due check %s %s failed", t.code, key)
                res["errors"].append(f"{key}: {e}")
    finally:
        c.close()
    return res


def check_all(db: Session, *, days: int, today: date | None = None) -> list[dict]:
    out = []
    for t in db.query(Tenant).filter(Tenant.active.is_(True)).order_by(Tenant.code).all():
        if not t.bexio_connected:
            out.append({"tenant": t.code, "kreditoren": [], "debitoren": [], "errors": ["bexio nicht verbunden"]})
            continue
        out.append(check_tenant(db, t, days=days, today=today))
    return out


def _line(i: dict) -> str:
    d = i["days_overdue"]
    when = f"{d} T überfällig" if d > 0 else ("heute fällig" if d == 0 else f"fällig in {-d} T")
    extra = " (im Zahlfile)" if i.get("payment_file") else ""
    ref = f" {i['ref']}" if i.get("ref") and i["kind"] == "kreditor" else ""
    return f"- {i['party']}{ref}: {i['currency']} {i['open']:.2f}, {when}{extra}"


def summary_text(results: list[dict]) -> str:
    """Push text, overdue first. Empty string if nothing is due."""
    blocks = []
    for r in results:
        lines = []
        for key, label in (("kreditoren", "Kreditoren"), ("debitoren", "Debitoren")):
            items = sorted(r[key], key=lambda x: -x["days_overdue"])
            if items:
                lines.append(f"{label}:")
                lines += [_line(i) for i in items]
        for e in r.get("errors", []):
            if e != "bexio nicht verbunden":
                lines.append(f"Fehler {e}")
        if lines:
            blocks.append(f"{r['tenant']}\n" + "\n".join(lines))
    return "\n\n".join(blocks)
