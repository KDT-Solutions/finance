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
