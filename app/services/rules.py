"""Supplier matching, rule lookup and the booking decision (deterministic part of the pipeline)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PaymentAccount, Rule, Supplier, Tenant

LEGAL_FORMS = {"ag", "gmbh", "sa", "sarl", "sagl", "ltd", "inc", "llc", "kg", "co", "corp", "plc", "bv", "nv", "se", "ug", "ohg", "limited", "pte", "oy", "ab", "as", "aps", "srl", "spa", "the"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in LEGAL_FORMS]
    return " ".join(tokens).strip()


def normalize_iban(iban: str | None) -> str:
    return (iban or "").replace(" ", "").upper()


def normalize_uid(uid: str | None) -> str:
    digits = re.sub(r"\D", "", uid or "")
    return f"CHE-{digits[0:3]}.{digits[3:6]}.{digits[6:9]}" if len(digits) == 9 else ""


def match_supplier(db: Session, tenant_id: int, *, iban: str = "", uid: str = "", name: str = "") -> tuple[Supplier | None, str]:
    """Returns (supplier, how) with how in iban|uid|name|fuzzy|''."""
    iban = normalize_iban(iban)
    uid = normalize_uid(uid)
    key = normalize_name(name)
    suppliers = db.query(Supplier).filter(Supplier.tenant_id == tenant_id).all()
    if iban:
        for s in suppliers:
            if iban in [normalize_iban(x) for x in (s.ibans or [])]:
                return s, "iban"
    if uid:
        for s in suppliers:
            if s.uid and normalize_uid(s.uid) == uid:
                return s, "uid"
    if key:
        for s in suppliers:
            if s.name_key == key or key in [normalize_name(v) for v in (s.name_variants or [])]:
                return s, "name"
        best, score = None, 0.0
        for s in suppliers:
            for cand in [s.name_key, *[normalize_name(v) for v in (s.name_variants or [])]]:
                if not cand:
                    continue
                if len(cand) >= 8 and (key.startswith(cand + " ") or cand.startswith(key + " ")):
                    return s, "name"
                r = SequenceMatcher(None, key, cand).ratio()
                if r > score:
                    best, score = s, r
        if best and score >= 0.9:
            return best, "fuzzy"
    return None, ""


def find_rule(supplier: Supplier | None, text: str) -> Rule | None:
    if not supplier:
        return None
    text_l = (text or "").lower()
    default = None
    for r in supplier.rules:
        if not r.active:
            continue
        kws = [k.lower() for k in (r.keywords or []) if k]
        if kws:
            if all(k in text_l for k in kws):
                return r
        elif default is None:
            default = r
    return default


@dataclass
class Decision:
    payment_mode: str = "open"  # open | card | private | bank_paid
    payment_account_id: int | None = None
    account_id: int | None = None
    tax_id: int | None = None
    booking_text: str = ""
    amount: float | None = None
    currency: str = "CHF"
    doc_date: str = ""
    due_date: str = ""
    document_number: str = ""
    supplier_name: str = ""
    move_to_topay: bool = False
    reasons: list[str] = field(default_factory=list)
    auto: bool = False
    matched_by: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _fmt_amount(v) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def decide(db: Session, tenant: Tenant, *, group: str, qr: dict | None, llm: dict, text: str,
           supplier: Supplier | None, rule: Rule | None, matched_by: str = "") -> Decision:
    s = get_settings()
    d = Decision(matched_by=matched_by)
    reasons: list[str] = []
    llm = llm or {}
    conf = llm.get("confidence") or {}

    # --- basics from extraction -----------------------------------------------------
    d.supplier_name = (qr or {}).get("creditor_name") or llm.get("supplier_name") or ""
    billing = (qr or {}).get("billing") if isinstance((qr or {}).get("billing"), dict) else {}
    d.document_number = llm.get("document_number") or billing.get("document_number", "")
    d.doc_date = llm.get("document_date") or billing.get("document_date", "")
    d.due_date = llm.get("due_date") or ""
    d.currency = ((qr or {}).get("currency") or llm.get("currency") or "CHF").upper()
    qr_amount = _fmt_amount((qr or {}).get("amount"))
    llm_amount = _fmt_amount(llm.get("total_gross"))
    d.amount = qr_amount if qr_amount else llm_amount

    doc_type = llm.get("document_type", "other")
    if not llm.get("is_receipt", True) or doc_type == "other":
        reasons.append("Beleg nicht als Rechnung/Quittung erkannt")
    if doc_type == "reminder":
        reasons.append("Mahnung, nie automatisch buchen")
    if doc_type == "credit_note":
        reasons.append("Gutschrift, manuell prüfen")
    if doc_type == "statement":
        reasons.append("Kontoauszug/Statement, manuell prüfen")
    if not d.amount:
        reasons.append("Betrag fehlt")
    if qr_amount and llm_amount and abs(qr_amount - llm_amount) > 0.05:
        reasons.append(f"QR-Betrag {qr_amount:.2f} weicht vom gelesenen Total {llm_amount:.2f} ab")
    if not d.doc_date:
        reasons.append("Belegdatum fehlt")
    if d.currency != "CHF":
        reasons.append(f"Fremdwährung {d.currency} (Phase 1 manuell)")
    for k in ("total_gross", "document_date"):
        if conf.get(k, 1.0) < s.auto_book_min_confidence:
            reasons.append(f"Konfidenz {k} nur {conf.get(k, 0):.2f}")
    vat_sum = sum(_fmt_amount(v.get("amount")) or 0 for v in llm.get("vat_lines") or [])
    net = _fmt_amount(llm.get("total_net"))
    if net and vat_sum and d.amount and abs(net + vat_sum - d.amount) > 0.06:
        reasons.append("Netto + MWST ergibt nicht das Total")

    # --- payment status from the Zammad group ---------------------------------------
    paid_hint = llm.get("payment_status") == "paid" or doc_type == "receipt"
    in_topay = group == tenant.zammad_group_topay
    if in_topay:
        d.payment_mode = "open"
        if paid_hint and llm.get("paid_hint"):
            reasons.append(f"Beleg in toPay trägt Bezahlt-Hinweis: {llm.get('paid_hint')}")
    else:
        if qr and not paid_hint:
            d.payment_mode = "open"
            d.move_to_topay = True
        elif paid_hint:
            d.payment_mode = "paid"
        else:
            reasons.append("Zahlstatus unklar (kein QR, kein Bezahlt-Hinweis)")

    # --- rule -------------------------------------------------------------------------
    if rule:
        d.account_id = rule.account_id
        d.tax_id = rule.tax_id
        d.booking_text = rule.booking_text or ""
        if d.payment_mode == "paid":
            if rule.payment_mode in ("card", "private", "bank_paid") and rule.payment_account_id:
                d.payment_mode = rule.payment_mode
                d.payment_account_id = rule.payment_account_id
        elif d.payment_mode == "open" and rule.payment_mode in ("card", "private") and not in_topay:
            reasons.append("Regel sagt bezahlt per Karte/privat, Beleg sieht nach offener Rechnung aus")
        if d.amount and rule.amount_min is not None and rule.amount_max is not None and not (rule.amount_min <= d.amount <= rule.amount_max):
            reasons.append(f"Betrag ausserhalb des üblichen Bands {rule.amount_min:.2f} bis {rule.amount_max:.2f}")
    else:
        d.account_id = tenant.default_expense_account_id
        d.tax_id = tenant.default_tax_id
        reasons.append("Neuer Lieferant ohne Regel" if not supplier else "Lieferant ohne Regel")
    if not d.account_id:
        reasons.append("Aufwandkonto fehlt")

    # --- paid without a payment account: try card last4 ----------------------------
    if d.payment_mode == "paid":
        last4 = (llm.get("card_last4") or "").strip()[-4:]
        accounts = db.query(PaymentAccount).filter(PaymentAccount.tenant_id == tenant.id, PaymentAccount.active.is_(True)).all()
        if last4:
            for a in accounts:
                if a.kind == "card" and a.card_last4 == last4:
                    d.payment_mode, d.payment_account_id = "card", a.id
                    break
        if d.payment_mode == "paid":
            reasons.append("Zahlkonto nicht bestimmbar (Karte/privat/Bank)")

    if llm.get("is_foreign_supplier") or (llm.get("supplier_country") and llm.get("supplier_country") != "CH"):
        if not rule:
            reasons.append("Ausländischer Lieferant (Bezugsteuer prüfen)")

    if not d.booking_text:
        d.booking_text = (llm.get("summary") or d.supplier_name or "")[:120]

    d.reasons = reasons
    d.auto = not reasons and rule is not None and rule.auto_book
    return d


def learn_from_decision(db: Session, tenant: Tenant, supplier: Supplier, rule: Rule | None, decision: dict, *, keywords: list[str] | None = None, source: str = "review") -> Rule:
    """Create or update the supplier rule from a confirmed booking."""
    amount = decision.get("amount")
    mode = decision.get("payment_mode") or "open"
    if rule is None:
        rule = Rule(tenant_id=tenant.id, supplier_id=supplier.id, keywords=keywords or [], learned_from=source)
        db.add(rule)
    changed = (rule.account_id, rule.tax_id, rule.payment_mode, rule.payment_account_id) != (
        decision.get("account_id"), decision.get("tax_id"), mode, decision.get("payment_account_id"))
    rule.account_id = decision.get("account_id")
    rule.tax_id = decision.get("tax_id")
    rule.booking_text = decision.get("booking_text") or rule.booking_text
    rule.payment_mode = mode
    rule.payment_account_id = decision.get("payment_account_id")
    if amount:
        lo = amount * 0.5
        hi = amount * 2.0
        rule.amount_min = min(rule.amount_min, lo) if rule.amount_min is not None else lo
        rule.amount_max = max(rule.amount_max, hi) if rule.amount_max is not None else hi
    if changed and (rule.hits or 0) > 0:
        rule.auto_book = False
        from ..models import now

        rule.last_corrected_at = now()
    else:
        rule.auto_book = True
    rule.hits = (rule.hits or 0) + 1
    rule.active = True
    db.add(rule)
    return rule
