from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)  # KDT, TAB, DDL, SPQ
    name: Mapped[str] = mapped_column(String(120))
    zammad_group_paid: Mapped[str] = mapped_column(String(80))  # Finance KDT
    zammad_group_topay: Mapped[str] = mapped_column(String(80))  # Finance toPay KDT
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # bexio OAuth (encrypted at rest)
    bexio_refresh_token: Mapped[str | None] = mapped_column(Text)
    bexio_access_token: Mapped[str | None] = mapped_column(Text)
    bexio_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bexio_company_name: Mapped[str | None] = mapped_column(String(160))
    # bexio company_id (aus /userinfo), beim ersten gueltigen Login festgehalten und danach bei
    # jedem Token-Refresh geprueft. Bleibt auch beim Trennen erhalten.
    bexio_company_id: Mapped[str | None] = mapped_column(String(40))
    bexio_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # bexio defaults (account ids from the chart of accounts)
    creditor_account_id: Mapped[int | None] = mapped_column(Integer)
    default_expense_account_id: Mapped[int | None] = mapped_column(Integer)
    fx_diff_account_id: Mapped[int | None] = mapped_column(Integer)
    fee_account_id: Mapped[int | None] = mapped_column(Integer)
    default_tax_id: Mapped[int | None] = mapped_column(Integer)
    # bexio bank account (3.0/banking/accounts id) used as paying account on purchase bills
    bank_account_id: Mapped[int | None] = mapped_column(Integer)
    # debtor data for payment files (pain.001): IBAN of the paying bank account, name as the bank knows it
    payment_iban: Mapped[str | None] = mapped_column(String(34))
    payment_name: Mapped[str | None] = mapped_column(String(70))
    # bexio user id used as contact_partner / owner when creating contacts
    bexio_user_id: Mapped[int | None] = mapped_column(Integer)
    # how to book paid receipts: "bill" (purchase bill + outgoing payment) or "entry" (manual entry)
    paid_mode: Mapped[str] = mapped_column(String(10), default="entry")

    # cached master data (accounts, taxes, bank accounts, currencies)
    master_data: Mapped[dict | None] = mapped_column(JSON)
    master_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    documents: Mapped[list[Document]] = relationship(back_populates="tenant")

    @property
    def bexio_connected(self) -> bool:
        return bool(self.bexio_refresh_token)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    tenant: Mapped[Tenant] = relationship()
    bexio_contact_id: Mapped[int | None] = mapped_column(Integer, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)  # normalised name
    uid: Mapped[str | None] = mapped_column(String(32), index=True)
    ibans: Mapped[list | None] = mapped_column(JSON, default=list)
    name_variants: Mapped[list | None] = mapped_column(JSON, default=list)
    is_foreign: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    rules: Mapped[list[Rule]] = relationship(back_populates="supplier")


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    tenant: Mapped[Tenant] = relationship()
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    # optional keywords in the document text that select this rule variant (empty = default)
    keywords: Mapped[list | None] = mapped_column(JSON, default=list)
    account_id: Mapped[int | None] = mapped_column(Integer)
    account_no: Mapped[str | None] = mapped_column(String(16))
    tax_id: Mapped[int | None] = mapped_column(Integer)
    tax_code: Mapped[str | None] = mapped_column(String(32))
    booking_text: Mapped[str | None] = mapped_column(String(200))
    # open | card | private | bank_paid
    payment_mode: Mapped[str] = mapped_column(String(16), default="open")
    payment_account_id: Mapped[int | None] = mapped_column(ForeignKey("payment_accounts.id"))
    amount_min: Mapped[float | None] = mapped_column(Float)
    amount_max: Mapped[float | None] = mapped_column(Float)
    auto_book: Mapped[bool] = mapped_column(Boolean, default=False)
    learned_from: Mapped[str | None] = mapped_column(String(40))  # history | review | manual
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    supplier: Mapped[Supplier] = relationship(back_populates="rules")
    payment_account: Mapped[PaymentAccount | None] = relationship()


class PaymentAccount(Base):
    __tablename__ = "payment_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    tenant: Mapped[Tenant] = relationship()
    label: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))  # bank | card | private
    card_last4: Mapped[str | None] = mapped_column(String(4))
    bexio_account_id: Mapped[int | None] = mapped_column(Integer)
    bexio_account_no: Mapped[str | None] = mapped_column(String(16))
    fee_account_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="uq_doc_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(400))
    size: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="zammad")  # zammad | upload
    zammad_ticket_id: Mapped[int | None] = mapped_column(Integer, index=True)
    zammad_ticket_number: Mapped[str | None] = mapped_column(String(32))
    zammad_article_id: Mapped[int | None] = mapped_column(Integer)
    zammad_group: Mapped[str | None] = mapped_column(String(80))
    ticket_title: Mapped[str | None] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    # new | extracted | needs_review | booked | skipped | error | cancelled | not_a_receipt
    state: Mapped[str] = mapped_column(String(20), default="new", index=True)
    review_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)

    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id"))
    # proposed/decided booking: account_id, tax_id, payment_mode, payment_account_id,
    # booking_text, amount, currency, doc_date, due_date, document_number, supplier_name
    decision: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    tenant: Mapped[Tenant] = relationship(back_populates="documents")
    supplier: Mapped[Supplier | None] = relationship()
    rule: Mapped[Rule | None] = relationship()
    extraction: Mapped[Extraction | None] = relationship(back_populates="document", uselist=False)
    booking: Mapped[Booking | None] = relationship(back_populates="document", uselist=False)


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), unique=True)
    qr: Mapped[dict | None] = mapped_column(JSON)
    llm: Mapped[dict | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    document: Mapped[Document] = relationship(back_populates="extraction")


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), unique=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    tenant: Mapped[Tenant] = relationship()
    kind: Mapped[str] = mapped_column(String(16))  # bill_open | bill_paid | entry
    bexio_bill_id: Mapped[str | None] = mapped_column(String(64))
    bexio_entry_id: Mapped[int | None] = mapped_column(Integer)
    bexio_file_id: Mapped[str | None] = mapped_column(String(64))
    bexio_payment_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSON)
    response: Mapped[dict | None] = mapped_column(JSON)
    amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | cancelled
    bexio_status: Mapped[str | None] = mapped_column(String(32))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # included in a pain.001 payment file (name + time), so it is not paid twice
    payment_file: Mapped[str | None] = mapped_column(String(80))
    payment_file_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    document: Mapped[Document] = relationship(back_populates="booking")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    actor: Mapped[str] = mapped_column(String(40))  # system | <user>
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSON)


class KV(Base):
    """Small key/value store for worker state (last poll per group, token for OAuth state)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


SEED_TENANTS = [
    ("KDT", "KDT-Solutions GmbH", "Finance KDT", "Finance toPay KDT"),
    ("TAB", "tabside GmbH", "Finance TAB", "Finance toPay TAB"),
    ("DDL", "Dodolock GmbH", "Finance DDL", "Finance toPay DDL"),
    ("SPQ", "Speqtrum Holding AG", "Finance SPQ", "Finance toPay SPQ"),
]


def seed_tenants(session_factory) -> None:
    with session_factory() as db:
        existing = {t.code for t in db.query(Tenant).all()}
        for code, name, paid, topay in SEED_TENANTS:
            if code not in existing:
                db.add(Tenant(code=code, name=name, zammad_group_paid=paid, zammad_group_topay=topay))
        db.commit()
