"""bexio API client per tenant: OAuth (authorization code + refresh), master data, contacts,
files, purchase bills (API 4.0), outgoing payments and manual entries (API 3.0).

Field names for the purchase-bill endpoints follow the bexio documentation as read in Oct 2026;
they are isolated in build_bill_payload() so they can be adjusted after the first real calls.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from .. import crypto
from ..config import get_settings
from . import pain001
from ..models import Tenant

log = logging.getLogger(__name__)


class BexioError(RuntimeError):
    def __init__(self, status: int, body: str, path: str = ""):
        super().__init__(f"bexio {status} on {path}: {body[:800]}")
        self.status = status
        self.body = body
        self.path = path


# --- OAuth -----------------------------------------------------------------------
def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/auth/bexio/callback"


def authorize_url(state: str) -> str:
    s = get_settings()
    q = {
        "client_id": s.bexio_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": s.bexio_scopes, "state": state,
    }
    return s.bexio_auth_url + "?" + urlencode(q)


def new_state(tenant_code: str) -> str:
    return f"{tenant_code}.{secrets.token_urlsafe(24)}"


def exchange_code(code: str) -> dict:
    s = get_settings()
    r = httpx.post(s.bexio_token_url, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": s.bexio_client_id, "client_secret": s.bexio_client_secret,
    }, timeout=30)
    if r.status_code >= 400:
        raise BexioError(r.status_code, r.text, "token")
    return r.json()


def _refresh(refresh_token: str) -> dict:
    s = get_settings()
    r = httpx.post(s.bexio_token_url, data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": s.bexio_client_id, "client_secret": s.bexio_client_secret,
    }, timeout=30)
    if r.status_code >= 400:
        raise BexioError(r.status_code, r.text, "refresh")
    return r.json()


def fetch_company(access_token: str) -> tuple[str, str]:
    """(company_id, company_name) des Logins laut bexio /userinfo (Scope company_profile)."""
    r = httpx.get(get_settings().bexio_userinfo_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code >= 400:
        raise BexioError(r.status_code, r.text, "userinfo")
    data = r.json()
    cid = str(data.get("company_id") or "").strip()
    if not cid:
        raise BexioError(403, "bexio liefert keine company_id (Scope company_profile fehlt) - bexio neu verbinden", "userinfo")
    return cid, str(data.get("company_name") or "")


def _norm(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def company_problem(tenant: Tenant, company: tuple[str, str]) -> str | None:
    """None, wenn die bexio-Firma zum Mandanten passt, sonst die Begruendung.
    Massgebend ist die gespeicherte company_id; ohne sie muss der Firmenname dem Mandantennamen entsprechen."""
    cid, cname = company
    label = f"{cname or 'unbekannt'} (ID {cid})"
    if tenant.bexio_company_id:
        if tenant.bexio_company_id != cid:
            return f"{tenant.code} ist mit bexio-Firma ID {tenant.bexio_company_id} verknüpft, angemeldet ist {label}."
        return None
    if _norm(cname) != _norm(tenant.name):
        return f"{tenant.code} erwartet die Firma '{tenant.name}', angemeldet ist {label}."
    return None


class CompanyMismatch(BexioError):
    def __init__(self, msg: str):
        super().__init__(403, msg, "company")


def store_tokens(db: Session, tenant: Tenant, tokens: dict) -> None:
    tenant.bexio_access_token = crypto.encrypt(tokens.get("access_token"))
    if tokens.get("refresh_token"):
        tenant.bexio_refresh_token = crypto.encrypt(tokens["refresh_token"])
    tenant.bexio_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    db.add(tenant)


def disconnect(db: Session, tenant: Tenant) -> None:
    tenant.bexio_access_token = None
    tenant.bexio_refresh_token = None
    tenant.bexio_token_expires_at = None
    tenant.bexio_connected_at = None
    db.add(tenant)


# --- client ----------------------------------------------------------------------
class BexioClient:
    def __init__(self, db: Session, tenant: Tenant):
        self.db = db
        self.tenant = tenant
        self.base = get_settings().bexio_api_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base, timeout=60)

    def close(self) -> None:
        self._http.close()

    def _token(self) -> str:
        t = self.tenant
        if not t.bexio_refresh_token:
            raise BexioError(401, "bexio nicht verbunden", "auth")
        exp = t.bexio_token_expires_at
        if t.bexio_access_token and exp and exp > datetime.now(timezone.utc):
            tok = crypto.decrypt(t.bexio_access_token)
            if tok:
                return tok
        tokens = _refresh(crypto.decrypt(t.bexio_refresh_token) or "")
        # bexio rotiert den Refresh Token: erst speichern, dann die Firma pruefen
        store_tokens(self.db, t, tokens)
        t.bexio_access_token = None  # erst nach bestandener Pruefung verwendbar
        self.db.commit()
        company = fetch_company(tokens["access_token"])
        problem = company_problem(t, company)
        if problem:
            disconnect(self.db, t)
            self.db.commit()
            log.error("bexio %s gesperrt: %s", t.code, problem)
            raise CompanyMismatch(f"{problem} Verbindung getrennt, bitte neu verbinden.")
        if not t.bexio_company_id:
            t.bexio_company_id = company[0]
        store_tokens(self.db, t, tokens)
        self.db.commit()
        return tokens["access_token"]

    def _req(self, method: str, path: str, *, json=None, params=None, files=None, data=None, retry: int = 2):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token()}"}
        r = self._http.request(method, path, json=json, params=params, files=files, data=data, headers=headers)
        if r.status_code == 429 and retry > 0:
            wait = int(r.headers.get("RateLimit-Reset", "5") or 5)
            time.sleep(min(max(wait, 1), 30))
            return self._req(method, path, json=json, params=params, files=files, data=data, retry=retry - 1)
        if r.status_code == 401 and retry > 0:
            self.tenant.bexio_access_token = None  # force refresh
            return self._req(method, path, json=json, params=params, files=files, data=data, retry=retry - 1)
        if r.status_code >= 400:
            raise BexioError(r.status_code, r.text, path)
        if not r.content:
            return None
        return r.json()

    def get(self, path: str, **params):
        return self._req("GET", path, params=params or None)

    def post(self, path: str, json=None, **kw):
        return self._req("POST", path, json=json, **kw)

    def put(self, path: str, json=None):
        return self._req("PUT", path, json=json)

    # --- master data ----------------------------------------------------------------
    def me(self) -> dict:
        return self.get("/3.0/users/me")

    def company_profile(self) -> dict:
        profiles = self.get("/2.0/company_profile")
        return profiles[0] if isinstance(profiles, list) and profiles else (profiles or {})

    def accounts(self) -> list[dict]:
        return self.get("/2.0/accounts", limit=2000) or []

    def taxes(self) -> list[dict]:
        return self.get("/3.0/taxes", limit=2000) or []

    def currencies(self) -> list[dict]:
        return self.get("/3.0/currencies") or []

    def exchange_rate(self, currency_code: str, date: str = "") -> float | None:
        """Rate of currency_code in CHF (bexio exchange rates of the currency, GET /3.0/currencies/{id}/exchange_rates?date=)."""
        code = (currency_code or "").upper()
        if code in ("", "CHF"):
            return 1.0
        cur = next((c for c in (self.tenant.master_data or {}).get("currencies", []) if c.get("code") == code), None)
        if not cur:
            cur = next(({"id": c["id"], "code": c.get("name")} for c in self.currencies() if c.get("name") == code), None)
        if not cur:
            return None
        params = {"date": date} if date else {}
        rates = self.get(f"/3.0/currencies/{cur['id']}/exchange_rates", **params) or []
        for r in rates:
            if (r.get("exchange_currency") or {}).get("name") == "CHF" and r.get("factor_nr"):
                return float(r["factor_nr"])
        return None

    def bank_accounts(self) -> list[dict]:
        return self.get("/3.0/banking/accounts") or []

    def refresh_master_data(self) -> dict:
        data = {
            "accounts": sorted(({"id": a["id"], "no": str(a.get("account_no", "")), "name": a.get("name", ""), "type": a.get("account_type")} for a in self.accounts()), key=lambda a: a["no"]),
            "taxes": [{"id": t["id"], "code": t.get("code", ""), "name": t.get("name") or t.get("display_name", ""), "rate": t.get("value") or t.get("percentage"), "type": t.get("type"), "active": t.get("is_active", True)} for t in self.taxes()],
            "currencies": [{"id": c["id"], "code": c.get("name") or c.get("currency_code", "")} for c in self.currencies()],
            "bank_accounts": [{"id": b["id"], "name": b.get("name", ""), "iban": b.get("iban", ""), "account_id": b.get("account_id")} for b in self.bank_accounts()],
        }
        try:
            me = self.me()
            self.tenant.bexio_user_id = me.get("id")
        except BexioError:
            pass
        try:
            self.tenant.bexio_company_name = self.company_profile().get("name")
        except BexioError:
            pass
        self.tenant.master_data = data
        self.tenant.master_data_at = datetime.now(timezone.utc)
        self.db.add(self.tenant)
        return data

    # --- contacts -------------------------------------------------------------------
    def search_contacts(self, name: str) -> list[dict]:
        return self.post("/2.0/contact/search", [{"field": "name_1", "value": name, "criteria": "like"}]) or []

    def create_contact(self, name: str, street: str = "", postcode: str = "", city: str = "", country_id: int | None = None) -> dict:
        user_id = self.tenant.bexio_user_id or self.me().get("id")
        body = {"contact_type_id": 1, "name_1": name[:80], "user_id": user_id, "owner_id": user_id}
        if street:
            body["street_name"] = street[:80]
        if postcode:
            body["postcode"] = postcode
        if city:
            body["city"] = city
        if country_id:
            body["country_id"] = country_id
        return self.post("/2.0/contact", body)

    # --- files ----------------------------------------------------------------------
    def upload_file(self, filename: str, content: bytes) -> dict:
        res = self._req("POST", "/3.0/files", files={"file": (filename, content, "application/pdf")}, data={"name": filename})
        if isinstance(res, list):
            return res[0]
        return res

    # --- purchase bills (API 4.0) ----------------------------------------------------
    def list_bills(self, **params) -> dict:
        """GET /4.0/purchase/bills. Filters per spec: limit (max 500), page (1-based), order, sort, search_term (min 3 chars),
        status (DRAFTS | TODO | PAID | OVERDUE), bill_date_start/_end, due_date_start/_end, vendor_ref, document_no, supplier_id ..."""
        if "status" in params and params["status"]:
            params["status"] = str(params["status"]).upper()
        return self.get("/4.0/purchase/bills", **params) or {}

    def find_bill(self, supplier_id: int | None, document_no: str) -> dict | None:
        if not document_no:
            return None
        params = {"vendor_ref": document_no, "limit": 50}
        if supplier_id:
            params["supplier_id"] = supplier_id
        res = self.list_bills(**params)
        for b in res.get("data", []) if isinstance(res, dict) else res:
            if (b.get("vendor_ref") or "") == document_no and (not supplier_id or b.get("supplier_id") == supplier_id):
                return b
        return None

    def create_bill(self, payload: dict) -> dict:
        return self.post("/4.0/purchase/bills", payload)

    def set_bill_status(self, bill_id: str, status: str) -> dict:
        """status: DRAFT | BOOKED (PUT /4.0/purchase/bills/{id}/bookings/{status}, no body)"""
        return self.put(f"/4.0/purchase/bills/{bill_id}/bookings/{status.upper()}", None)

    def get_bill(self, bill_id: str) -> dict:
        return self.get(f"/4.0/purchase/bills/{bill_id}")

    def update_bill(self, bill_id: str, payload: dict) -> dict:
        """PUT /4.0/purchase/bills/{id}; required: supplier_id, contact_partner_id, manual_amount, currency_code, address,
        bill_date, due_date, line_items, item_net, split_into_line_items, discounts, attachment_ids."""
        return self.put(f"/4.0/purchase/bills/{bill_id}", payload)

    def bill_update_payload(self, bill: dict) -> dict:
        """Turn a GET bill response into a PUT body (read-only fields dropped)."""
        keys = ("document_no", "title", "supplier_id", "vendor_ref", "amount_man", "amount_calc", "manual_amount", "contact_partner_id",
                "bill_date", "due_date", "currency_code", "exchange_rate", "item_net", "split_into_line_items", "base_currency_amount",
                "attachment_ids", "address", "line_items", "discounts", "payment")
        out = {k: bill[k] for k in keys if k in bill and bill[k] is not None}
        out.setdefault("split_into_line_items", False)
        out.setdefault("discounts", [])
        out.setdefault("attachment_ids", [])
        out["line_items"] = [{k: li[k] for k in ("id", "position", "title", "tax_id", "amount", "booking_account_id") if k in li and li[k] is not None}
                             for li in bill.get("line_items", [])]
        if out.get("payment"):
            pay = out["payment"]
            out["payment"] = {k: pay[k] for k in ("type", "bank_account_id", "fee", "execution_date", "exchange_rate", "amount", "iban", "name",
                                                   "address", "street", "house_no", "postcode", "city", "country_code", "message", "booking_text",
                                                   "salary_payment", "reference_no", "note") if k in pay and pay[k] is not None}
            out["payment"].setdefault("salary_payment", False)
        return out

    def create_outgoing_payment(self, payload: dict) -> dict:
        return self.post("/4.0/purchase/outgoing-payments", payload)

    # --- manual entries (API 3.0) ----------------------------------------------------
    def create_manual_entry(self, date: str, reference_nr: str, entries: list[dict]) -> dict:
        """One line: manual_single_entry, several lines (e.g. one per VAT rate): manual_compound_entry."""
        kind = "manual_compound_entry" if len(entries) > 1 else "manual_single_entry"
        return self.post("/3.0/accounting/manual_entries", {"type": kind, "date": date, "reference_nr": reference_nr[:40], "entries": entries})

    def attach_file_to_entry(self, manual_entry_id: int, entry_id: int, filename: str, content: bytes) -> dict:
        return self._req("POST", f"/3.0/accounting/manual_entries/{manual_entry_id}/entries/{entry_id}/files",
                         files={"file": (filename, content, "application/pdf")})

    def journal(self, date_from: str, date_to: str) -> list[dict]:
        return self.get("/3.0/accounting/journal", **{"from": date_from, "to": date_to}) or []


# --- payload builders ---------------------------------------------------------------
def build_bill_payload(*, supplier_id: int, contact_partner_id: int | None, supplier_name: str, document_no: str, title: str,
                       bill_date: str, due_date: str, currency: str, amount_gross: float, account_id: int,
                       tax_id: int | None, text: str, attachment_ids: list[str], qr: dict | None,
                       bank_account_id: int | None = None, exchange_rate: float | None = None, supplier_iban: str = "",
                       lines: list[dict] | None = None) -> dict:
    """POST /4.0/purchase/bills body per the bexio OpenAPI spec (required: supplier_id, contact_partner_id, currency_code,
    address, bill_date, due_date, manual_amount, item_net, line_items, discounts, attachment_ids; amount_calc when
    manual_amount is false). Line items carry 'title', not 'text'. Payment: type IBAN | QR | MANUAL with
    execution_date, amount, salary_payment required; iban, name, reference_no (27), message (140), bank_account_id.
    lines: several positions (e.g. one per VAT rate), each {title, amount (gross), account_id, tax_id}; else one line.
    Positions with "net" (customs invoices) are sent net with item_net, like bills booked by hand in bexio: the import
    VAT position (ZOLLM/ZOLLB, 100%) carries the tax itself as amount."""
    amount = round(float(amount_gross), 2)
    due = due_date or bill_date
    items = []
    item_net = bool(lines) and all(ln.get("net") is not None for ln in lines)
    for i, ln in enumerate(lines or [{"title": "", "amount": amount, "account_id": account_id, "tax_id": tax_id}]):
        item = {
            "position": i,
            "title": (ln.get("title") or text or title or document_no or "Rechnung")[:80],
            "amount": round(float(ln["net"] if item_net else ln["amount"]), 2),
            "booking_account_id": ln.get("account_id") or account_id,
        }
        if ln.get("tax_id"):
            item["tax_id"] = ln["tax_id"]
        items.append(item)
    head = " | ".join(x for x in ((supplier_name or "").strip(), (document_no or "").strip()) if x) or (title or "Rechnung")
    payload = {
        "supplier_id": supplier_id,
        "title": head[:80],
        "bill_date": bill_date,
        "due_date": due,
        "currency_code": currency or "CHF",
        "manual_amount": False,
        "amount_calc": amount,
        "item_net": item_net,
        "address": {
            "lastname_company": (supplier_name or "Lieferant")[:80],
            "type": "COMPANY",
            "main_contact_id": supplier_id,
        },
        "line_items": items,
        "discounts": [],
        "attachment_ids": list(attachment_ids or []),
    }
    if (currency or "CHF").upper() != "CHF":
        # required by bexio for foreign-currency bills
        if not exchange_rate:
            raise BexioError(f"Kein Wechselkurs für {currency}")
        payload["exchange_rate"] = round(float(exchange_rate), 10)
        payload["base_currency_amount"] = round(amount * float(exchange_rate), 2)
    if contact_partner_id:
        payload["contact_partner_id"] = contact_partner_id
    if document_no:
        payload["vendor_ref"] = document_no[:255]
    if qr and qr.get("iban"):
        iban = qr["iban"].replace(" ", "")
        is_qr = bool(qr.get("is_qr_iban") or qr.get("reference_type") == "QRR")
        pay = {
            "type": "QR" if is_qr else "IBAN",
            "execution_date": due,
            "amount": amount,
            "salary_payment": False,
            "iban": iban,
        }
        if qr.get("creditor_name"):
            pay["name"] = qr["creditor_name"][:70]
        if qr.get("reference") and qr.get("reference_type") in ("QRR", "SCOR"):
            pay["reference_no"] = qr["reference"].replace(" ", "")[:27]
        if qr.get("message"):
            pay["message"] = qr["message"][:140]
        if bank_account_id:
            pay["bank_account_id"] = bank_account_id
        payload["payment"] = pay
        if qr.get("billing_info"):
            payload["qr_bill_information"] = qr["billing_info"][:255]
    elif supplier_iban and pain001.iban_valid(supplier_iban) and not pain001.is_qr_iban(supplier_iban):
        # invoice without QR part (e.g. foreign supplier): IBAN payment, invoice number as message
        pay = {
            "type": "IBAN",
            "execution_date": due,
            "amount": amount,
            "salary_payment": False,
            "iban": supplier_iban.replace(" ", "").upper(),
            "name": (supplier_name or "")[:70] or "Lieferant",
        }
        if document_no:
            pay["message"] = f"Rechnung {document_no}"[:140]
        if bank_account_id:
            pay["bank_account_id"] = bank_account_id
        payload["payment"] = pay
    return payload


def build_manual_entry_split(*, lines: list[dict], debit_account_id: int, credit_account_id: int, description: str) -> list[dict]:
    """Paid receipt with several positions (e.g. one per VAT rate): one compound-entry line each against the payment account."""
    out = []
    for ln in lines:
        acc = ln.get("account_id") or debit_account_id
        text = " ".join(x for x in (description, ln.get("title") or "") if x)
        out += build_manual_entry_lines(amount=ln["amount"], debit_account_id=acc, credit_account_id=credit_account_id,
                                        tax_id=ln.get("tax_id"), tax_account_id=acc, description=text)
    return out


def build_manual_entry_lines(*, amount: float, debit_account_id: int, credit_account_id: int, tax_id: int | None,
                             tax_account_id: int | None, description: str, currency_id: int = 1, currency_factor: float = 1.0) -> list[dict]:
    line = {
        "debit_account_id": debit_account_id,
        "credit_account_id": credit_account_id,
        "amount": round(float(amount), 2),
        "description": description[:200],
        "currency_id": currency_id,
        "currency_factor": currency_factor,
    }
    if tax_id:
        line["tax_id"] = tax_id
        line["tax_account_id"] = tax_account_id or debit_account_id
    return [line]
