"""Find and clean up duplicate bexio contacts (e.g. created by an earlier receipt tool).

bexio has no merge function and paid bills cannot be moved to another contact. So per duplicate group one main
contact is kept and each duplicate is handled by what is linked to it:
  archive  - nothing linked: the contact is archived
  move     - only open/draft purchase bills: the bills are moved to the main contact, then the contact is archived
  mark     - paid bills, expenses or sales documents: the contact stays and gets a name suffix so nobody picks it
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import Supplier, Tenant
from . import bexio as bx
from .rules import normalize_name

log = logging.getLogger(__name__)

MARK = " (Duplikat, nicht verwenden)"
PAID_STATES = {"PAID", "PARTIALLY_PAID"}


def contact_name(c: dict) -> str:
    return " ".join(x for x in (c.get("name_1"), c.get("name_2")) if x).strip()


def all_contacts(client: bx.BexioClient) -> list[dict]:
    out, offset = [], 0
    while True:
        page = client.get("/2.0/contact", limit=2000, offset=offset) or []
        out.extend(page)
        if len(page) < 2000:
            return out
        offset += 2000


def _paged(client: bx.BexioClient, path: str, **params) -> list[dict]:
    rows, page = [], 1
    while True:
        res = client.get(path, limit=500, page=page, **params) or {}
        data = res.get("data", []) if isinstance(res, dict) else res
        rows.extend(data)
        if len(data) < 500:
            return rows
        page += 1


def linked(client: bx.BexioClient, contact_id: int) -> dict:
    bills = _paged(client, "/4.0/purchase/bills", supplier_id=contact_id)
    bills = [b for b in bills if b.get("supplier_id") in (None, contact_id)]
    expenses = _paged(client, "/4.0/expenses", supplier_id=contact_id)
    expenses = [e for e in expenses if e.get("supplier_id") in (None, contact_id)]
    sales = 0
    for kind in ("kb_invoice", "kb_offer", "kb_order"):
        sales += len(client.post(f"/2.0/{kind}/search", [{"field": "contact_id", "value": str(contact_id), "criteria": "="}], params={"limit": 50}) or [])
    paid = [b for b in bills if str(b.get("status", "")).upper() in PAID_STATES]
    open_ = [b for b in bills if b not in paid]
    return {"bills_open": [{"id": b["id"], "document_no": b.get("document_no"), "vendor_ref": b.get("vendor_ref"), "status": b.get("status"),
                            "gross": b.get("gross"), "currency": b.get("currency_code")} for b in open_],
            "bills_paid": len(paid), "expenses": len(expenses), "sales_documents": sales}


def analyze(db: Session, client: bx.BexioClient, tenant: Tenant) -> list[dict]:
    contacts = all_contacts(client)
    groups: dict[str, list[dict]] = {}
    for c in contacts:
        key = normalize_name(contact_name(c))
        if key and not contact_name(c).endswith(MARK.strip()):
            groups.setdefault(key, []).append(c)
    used = {s.bexio_contact_id for s in db.query(Supplier).filter(Supplier.tenant_id == tenant.id, Supplier.bexio_contact_id.isnot(None))}
    out = []
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        rows = []
        for c in sorted(members, key=lambda c: c["id"]):
            info = linked(client, c["id"])
            rows.append({"id": c["id"], "nr": c.get("nr"), "name": contact_name(c), "used_by_finance": c["id"] in used, **info})
        # main contact: the one finance uses, then the one with the most documents, then the oldest
        main = sorted(rows, key=lambda r: (not r["used_by_finance"],
                                           -(r["bills_paid"] + len(r["bills_open"]) + r["expenses"] + r["sales_documents"]), r["id"]))[0]
        out.append(plan({"key": key, "contacts": rows}, main["id"]))
    return out


def plan(group: dict, main_id: int) -> dict:
    if main_id not in [r["id"] for r in group["contacts"]]:
        raise ValueError(f"Kontakt {main_id} gehört nicht zur Gruppe {group['key']}")
    group["main_contact_id"] = main_id
    for r in group["contacts"]:
        if r["id"] == main_id:
            r["action"] = "main"
        elif r["bills_paid"] or r["expenses"] or r["sales_documents"]:
            r["action"] = "mark"
        elif r["bills_open"]:
            r["action"] = "move"
        else:
            r["action"] = "archive"
    return group


def _move_bill(client: bx.BexioClient, bill_id: str, main: dict) -> str:
    bill = client.get_bill(bill_id)
    status = str(bill.get("status", "")).upper()
    if status in PAID_STATES:
        raise bx.BexioError(400, "bezahlte Rechnung kann nicht umgehängt werden", bill_id)
    if status != "DRAFT":
        client.set_bill_status(bill_id, "DRAFT")
        bill = client.get_bill(bill_id)
    body = client.bill_update_payload(bill)
    body["supplier_id"] = main["id"]
    addr = dict(body.get("address") or {})
    addr.pop("contact_address_id", None)
    addr["main_contact_id"] = main["id"]
    addr["lastname_company"] = (main.get("name_1") or contact_name(main))[:80]
    body["address"] = addr
    client.update_bill(bill_id, body)
    if status != "DRAFT":
        client.set_bill_status(bill_id, "BOOKED")
    return status


def archive_contact(client: bx.BexioClient, contact_id: int) -> None:
    """DELETE /2.0/contact/{id} only marks the contact (bexio: archived, restorable via PATCH .../restore)."""
    client._req("DELETE", f"/2.0/contact/{contact_id}")


def mark_contact(client: bx.BexioClient, contact: dict) -> str:
    name = (contact.get("name_1") or "").strip()
    if name.endswith(MARK.strip()):
        return name
    new = (name[: 80 - len(MARK)] + MARK)
    body = {"name_1": new, "contact_type_id": contact.get("contact_type_id"), "user_id": contact.get("user_id"), "owner_id": contact.get("owner_id")}
    client.post(f"/2.0/contact/{contact['id']}", {k: v for k, v in body.items() if v is not None})
    return new


def clean_group(db: Session, client: bx.BexioClient, tenant: Tenant, group: dict) -> list[dict]:
    """Execute the planned action for each duplicate of one analysed group. Each contact is handled on its own;
    an error stops only that contact."""
    main = client.get(f"/2.0/contact/{group['main_contact_id']}")
    results = []
    for r in group["contacts"]:
        if r["action"] == "main":
            continue
        res = {"id": r["id"], "name": r["name"], "action": r["action"], "ok": False}
        try:
            if r["action"] == "move":
                res["moved_bills"] = []
                for b in r["bills_open"]:
                    _move_bill(client, b["id"], main)
                    res["moved_bills"].append(b.get("document_no") or b["id"])
                archive_contact(client, r["id"])
            elif r["action"] == "archive":
                archive_contact(client, r["id"])
            elif r["action"] == "mark":
                res["new_name"] = mark_contact(client, client.get(f"/2.0/contact/{r['id']}"))
            for s in db.query(Supplier).filter(Supplier.tenant_id == tenant.id, Supplier.bexio_contact_id == r["id"]):
                s.bexio_contact_id = main["id"]
            db.commit()
            res["ok"] = True
        except bx.BexioError as e:
            log.warning("duplicate cleanup %s failed: %s", r["id"], e)
            res["error"] = str(e)[:300]
        results.append(res)
    return results
