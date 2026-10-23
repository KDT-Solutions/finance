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

    def bank_accounts(self) -> list[dict]:
        return self.get("/3.0/banking/accounts") or []

    def refresh_master_data(self) -> dict:
        data = {
            "accounts": [{"id": a["id"], "no": str(a.get("account_no", "")), "name": a.get("name", ""), "type": a.get("account_type")} for a in self.accounts()],
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

    def create_outgoing_payment(self, payload: dict) -> dict:
        return self.post("/4.0/purchase/outgoing-payments", payload)

    # --- manual entries (API 3.0) ----------------------------------------------------
    def create_manual_entry(self, date: str, reference_nr: str, entries: list[dict]) -> dict:
        return self.post("/3.0/accounting/manual_entries", {"type": "manual_single_entry", "date": date, "reference_nr": reference_nr[:40], "entries": entries})

    def attach_file_to_entry(self, manual_entry_id: int, entry_id: int, filename: str, content: bytes) -> dict:
        return self._req("POST", f"/3.0/accounting/manual_entries/{manual_entry_id}/entries/{entry_id}/files",
                         files={"file": (filename, content, "application/pdf")})

    def journal(self, date_from: str, date_to: str) -> list[dict]:
        return self.get("/3.0/accounting/journal", **{"from": date_from, "to": date_to}) or []


# --- payload builders ---------------------------------------------------------------
def build_bill_payload(*, supplier_id: int, contact_partner_id: int | None, supplier_name: str, document_no: str, title: str,
                       bill_date: str, due_date: str, currency: str, amount_gross: float, account_id: int,
                       tax_id: int | None, text: str, attachment_ids: list[str], qr: dict | None,
                       bank_account_id: int | None = None) -> dict:
    """POST /4.0/purchase/bills body per the bexio OpenAPI spec (required: supplier_id, contact_partner_id, currency_code,
    address, bill_date, due_date, manual_amount, item_net, line_items, discounts, attachment_ids; amount_calc when
    manual_amount is false). Line items carry 'title', not 'text'. Payment: type IBAN | QR | MANUAL with
    execution_date, amount, salary_payment required; iban, name, reference_no (27), message (140), bank_account_id."""
    amount = round(float(amount_gross), 2)
    due = due_date or bill_date
    line = {
        "position": 0,
        "title": (text or title or document_no or "Rechnung")[:80],
        "amount": amount,
        "booking_account_id": account_id,
    }
    if tax_id:
        line["tax_id"] = tax_id
    payload = {
        "supplier_id": supplier_id,
        "title": (title or document_no or "Rechnung")[:80],
        "bill_date": bill_date,
        "due_date": due,
        "currency_code": currency or "CHF",
        "manual_amount": False,
        "amount_calc": amount,
        "item_net": False,
        "address": {
            "lastname_company": (supplier_name or "Lieferant")[:80],
            "type": "COMPANY",
            "main_contact_id": supplier_id,
        },
        "line_items": [line],
        "discounts": [],
        "attachment_ids": list(attachment_ids or []),
    }
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
    return payload


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
