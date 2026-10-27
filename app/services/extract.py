"""Structured extraction of invoice/receipt data with the Claude API (PDF passed as document)."""

from __future__ import annotations

import base64
import logging

from ..config import get_settings

log = logging.getLogger(__name__)

PROMPT_VERSION = "4"

SCHEMA = {
    "type": "object",
    "properties": {
        "is_receipt": {"type": "boolean", "description": "true if the document is an invoice, receipt, credit note or reminder addressed to the recipient company; false for order confirmations, terms, delivery notes, ads, newsletters."},
        "document_type": {"type": "string", "enum": ["invoice", "receipt", "credit_note", "reminder", "statement", "other"]},
        "supplier_name": {"type": "string"},
        "supplier_uid": {"type": "string", "description": "Swiss UID like CHE-123.456.789 or empty"},
        "supplier_country": {"type": "string", "description": "ISO 3166-1 alpha-2"},
        "supplier_street": {"type": "string", "description": "street and number of the supplier, or empty"},
        "supplier_postcode": {"type": "string"},
        "supplier_city": {"type": "string"},
        "supplier_iban": {"type": "string", "description": "IBAN printed on the document, or empty"},
        "recipient_name": {"type": "string", "description": "company the document is addressed to"},
        "document_number": {"type": "string"},
        "document_date": {"type": "string", "description": "YYYY-MM-DD"},
        "due_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "service_period_from": {"type": "string"},
        "service_period_to": {"type": "string"},
        "currency": {"type": "string", "description": "ISO 4217"},
        "total_gross": {"type": "number"},
        "total_net": {"type": "number"},
        "vat_lines": {"type": "array", "description": "one entry per VAT rate from the VAT summary (e.g. 2.6% and 8.1% on one invoice): "
                      "rate in percent, base = net amount taxed at that rate, amount = VAT; include a 0% rate only if printed", "items": {"type": "object", "properties": {"rate": {"type": "number"}, "base": {"type": "number"}, "amount": {"type": "number"}}, "required": ["rate", "amount"]}},
        "line_items": {"type": "array", "items": {"type": "object", "properties": {"description": {"type": "string"}, "amount_gross": {"type": "number"}, "vat_rate": {"type": "number"}}, "required": ["description", "amount_gross"]}},
        "customs": {"type": "object", "description": "only for import / customs invoices of couriers and forwarders (DHL, UPS, FedEx, "
                    "Swiss Post 'Rechnung MWST / Verzollungskosten'), else omit: import_vat = import VAT paid for the goods "
                    "('MwSt / TVA / IVA', 'Einfuhrsteuer'), customs_duty = 'Einfuhrzoll' plus other duties ('Sonstige Abgaben'), "
                    "services_net = courier fees without VAT (e.g. Kontoabwicklungsgebühr, Vorlagegebühr), services_vat = VAT on those fees",
                    "properties": {"import_vat": {"type": "number"}, "customs_duty": {"type": "number"},
                                   "services_net": {"type": "number"}, "services_vat": {"type": "number"}}},
        "is_commission": {"type": "boolean", "description": "true if the document credits the recipient a commission, partner "
                          "provision or referral fee (e.g. 'Partnerprovision', 'Gutschrift Provision'), i.e. income for the recipient"},
        "payment_status": {"type": "string", "enum": ["unpaid", "paid", "unknown"]},
        "paid_hint": {"type": "string", "description": "verbatim hint such as 'bezahlt mit Visa ****1234' or 'Zahlungsbestätigung', else empty"},
        "payment_method_hint": {"type": "string", "enum": ["card", "bank_transfer", "paypal", "twint", "direct_debit", "none", "unknown"]},
        "card_last4": {"type": "string", "description": "last 4 digits of a card if printed, else empty"},
        "has_qr_bill": {"type": "boolean", "description": "true if a Swiss QR payment part is visible"},
        "summary": {"type": "string", "description": "one line: what was bought, in German"},
        "confidence": {"type": "object", "properties": {"total_gross": {"type": "number"}, "document_date": {"type": "number"}, "supplier_name": {"type": "number"}, "payment_status": {"type": "number"}}, "required": ["total_gross", "document_date", "supplier_name", "payment_status"]},
    },
    "required": ["is_receipt", "document_type", "supplier_name", "document_number", "document_date", "currency", "total_gross", "payment_status", "payment_method_hint", "confidence"],
}

SYSTEM = (
    "You extract accounting data from Swiss supplier documents (invoices, receipts, credit notes) for bookkeeping. "
    "Read carefully, never guess numbers: if a value is not on the document, leave it empty or 0 and lower the confidence. "
    "Amounts are gross totals as printed. Dates in ISO format. A 'Zahlungsbestätigung', 'Quittung', 'Receipt', "
    "'bezahlt', 'paid' or a card/PayPal/TWINT reference means payment_status=paid. An invoice with a Swiss QR payment "
    "part, 'Zahlbar bis', 'payable within' or an IBAN and no paid hint means unpaid. Confidence values are 0..1."
)


def extract(pdf_bytes: bytes, context: str = "") -> tuple[dict, str]:
    """Returns (data, model). Raises on API errors."""
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    content: list[dict] = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
        {"type": "text", "text": "Extract the document data. " + (f"Context from the e-mail: {context[:1500]}" if context else "")},
    ]
    # Forced tool use (tool_choice type "tool"/"any") is not supported by every model, so the tool is offered with
    # "auto" and the instruction makes the call mandatory; a JSON text answer is accepted as fallback.
    content[-1]["text"] += " Always answer by calling the record_document tool exactly once, with no other text."
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=2000,
        system=SYSTEM,
        tools=[{"name": "record_document", "description": "Record the extracted document data. Must be called exactly once per document.", "input_schema": SCHEMA}],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        messages=[{"role": "user", "content": content}],
    )
    text_parts: list[str] = []
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_document":
            return dict(block.input), resp.model
        if block.type == "text":
            text_parts.append(block.text)
    data = _json_from_text("\n".join(text_parts))
    if data is not None:
        return data, resp.model
    raise RuntimeError("no tool_use block in LLM response")


def _json_from_text(text: str) -> dict | None:
    import json
    import re

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "is_receipt" in data else None
