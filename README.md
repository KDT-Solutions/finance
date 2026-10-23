# finance

Self-hosted receipt processing for bexio, multi-tenant (one bexio company per tenant).
Replaces a hosted document-capture service: supplier invoices and receipts arrive as PDF
in Zammad tickets, the tool reads them, decides how to book them and writes to bexio.

1. **Ingest** – the worker polls the Finance groups in Zammad (`Finance <X>` and
   `Finance toPay <X>` per tenant), downloads new PDF attachments, deduplicates by hash.
2. **Read** – the Swiss QR-bill is decoded deterministically (IBAN, reference, amount);
   everything else comes from a Claude extraction with a fixed JSON schema.
3. **Decide** – supplier matching (IBAN, UID, name), supplier rules (account, tax code,
   payment mode, amount band), plausibility checks. The Zammad group is the payment
   signal: `toPay` = unpaid, `Finance` = not sorted yet (the tool moves unpaid invoices
   to `toPay` itself).
4. **Book** – unpaid invoice → purchase bill in bexio (open, settled by the bank sync);
   paid receipt → manual entry expense account / payment account with the PDF attached.
   Everything else lands in the review queue; each confirmed decision becomes a rule.
5. **Feedback** – internal note in the ticket, ticket closed for paid receipts, open
   invoices are closed once bexio reports the bill as paid.

## Stack

FastAPI + SQLAlchemy + Postgres, a worker process, server-rendered UI. Containers:
`app`, `worker`, `db`. PDFs are stored on the `finance-data` volume.

## Running

```
cp .env.example .env   # fill in the values
docker compose up -d   # image comes from ghcr.io, built by the GitHub Actions workflow
```

The UI listens on `HOST_PORT` (default 8436, bound to `BIND_ADDR`). Put a reverse proxy
with TLS in front and set `PUBLIC_BASE_URL` to the public URL (used for the bexio
redirect URL and the CSRF origin check).

Secrets:

```
openssl rand -hex 32                     # SECRET_KEY
python -m app.tools fernet-key           # DATA_ENCRYPTION_KEY
python -m app.tools hash-password        # ADMIN_PASSWORD_HASH_B64
python -m app.tools totp-secret          # ADMIN_TOTP_SECRET (optional)
```

### External services

| Service | Needed for | Where it goes |
|---|---|---|
| Zammad API token (agent with access to the Finance groups) | Ingest, notes, group/state | `ZAMMAD_URL`, `ZAMMAD_TOKEN` |
| Anthropic API key | Document extraction | `ANTHROPIC_API_KEY` |
| bexio developer app (client id/secret, redirect URL `<PUBLIC_BASE_URL>/auth/bexio/callback`) | Booking | `BEXIO_CLIENT_ID`, `BEXIO_CLIENT_SECRET`; each tenant is connected in the UI (OAuth with refresh token) |
| Pushover (optional) | Daily summary of open todos | `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` |

### First setup

1. Settings: connect each tenant with bexio, load master data, set default expense
   account and tax code.
2. Payment accounts: add credit cards (last 4 digits) and the private account with
   their bexio contra accounts.
3. Settings: "learn rules from bexio history" per tenant.
4. Todo: review the first documents, confirm with "remember rule"; known suppliers are
   then booked without a click.

## bexio API notes

Purchase bills use API 4.0 (`POST /4.0/purchase/bills`, `PUT /4.0/purchase/bills/{id}/bookings/{status}`).
The payload builder lives in `app/services/bexio.py` (`build_bill_payload`) and follows the OpenAPI spec embedded in docs.bexio.com; adjust field
names there if bexio rejects the first real call (the error is shown on the document page).
Manual entries use API 3.0 (`POST /3.0/accounting/manual_entries` + file attach on the line).
