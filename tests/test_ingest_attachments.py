"""Stripe-Mails: Invoice und Receipt zur selben Zahlung, nur die Invoice wird eingelesen."""
from app.services.pipeline import invoices_over_receipts
from app.services.zammad import Attachment


def _att(article_id, attachment_id, name):
    return Attachment(1, article_id, attachment_id, name, 1000, "application/pdf")


def test_receipt_dropped_when_invoice_in_same_article():
    atts = [_att(10, 1, "Invoice-9BF0758D-10522062.pdf"), _att(10, 2, "Receipt-2735-1642-3064.pdf")]
    assert [a.filename for a in invoices_over_receipts(atts)] == ["Invoice-9BF0758D-10522062.pdf"]


def test_receipt_alone_is_kept():
    atts = [_att(10, 2, "Receipt-2735-1642-3064.pdf")]
    assert invoices_over_receipts(atts) == atts


def test_receipt_in_other_article_is_kept():
    atts = [_att(10, 1, "Invoice-A.pdf"), _att(11, 2, "Receipt-B.pdf")]
    assert invoices_over_receipts(atts) == atts


def test_other_pdfs_untouched():
    atts = [_att(10, 1, "Invoice-A.pdf"), _att(10, 2, "AGB.pdf"), _att(10, 3, "receipt-a.PDF")]
    assert [a.filename for a in invoices_over_receipts(atts)] == ["Invoice-A.pdf", "AGB.pdf"]


class _FakeZammad:
    """One open ticket in the paid group: the mail and its forward carry the same PDF (sipcall Ticket#2742811)."""

    def __init__(self, paid_group):
        self.paid_group = paid_group
        self.notes = []

    def open_tickets_in_group(self, group, limit=20):
        if group != self.paid_group:
            return []
        return [{"id": 50804, "number": "2742811", "title": "sipcall-Partnerprovision: September 2026", "updated_at": "u1", "article_count": 2}]

    def pdf_attachments(self, tid):
        return [Attachment(tid, 79827, 132960, "Provision_932_2609-1.pdf", 100, "application/pdf"),
                Attachment(tid, 79834, 132976, "Provision_932_2609-1.pdf", 100, "")]

    def download(self, att):
        return b"%PDF-1.4 provision"

    def add_note(self, tid, body):
        self.notes.append((tid, body))


def test_same_pdf_at_mail_and_forward_is_ingested_once(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.db import Base
    from app.models import Document, Tenant
    from app.services import pipeline

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()  # as app.db.SessionLocal
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    t = Tenant(code="AAA", name="Beispiel AAA GmbH", zammad_group_paid="Finance AAA", zammad_group_topay="Finance toPay AAA")
    db.add(t)
    db.commit()
    z = _FakeZammad("Finance AAA")
    assert pipeline.ingest_tenant(db, z, t) == 1
    docs = db.query(Document).all()
    assert len(docs) == 1 and docs[0].zammad_article_id == 79827 and z.notes == []
    # the ticket is marked as seen, the next run does nothing
    assert pipeline.ingest_tenant(db, z, t) == 0
