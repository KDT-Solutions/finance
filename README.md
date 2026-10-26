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
   invoices are closed once bexio reports the bill as paid. An invoice put into a payment
   file sets its ticket to "pending reminder" for 14 days (`PAYMENT_WAIT_DAYS`, 09:00 local);
   the paid check closes it once bexio reports the bill as paid, otherwise it waits another
   14 days after the reminder is due. A payment file that was not uploaded can be regenerated with
   the current data, regenerated without one invoice (that ticket is reopened), or withdrawn
   (all payments open again). The old file is locked in every case (renamed to `.void`, no
   download); all three are refused when a bill in the file is already paid in bexio.

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

## bexio company check

Each tenant only accepts its own bexio company. After the OAuth login the server asks bexio
`/userinfo` (scope `company_profile`) which company was signed in. Without a stored company the
company name must equal the tenant name (case and spacing ignored); the `company_id` is then stored
and is the only thing that counts afterwards. A login with another company is rejected and the
existing connection stays. The id is checked again on every token refresh; on a mismatch the
tenant is disconnected. Connections made before this check (without `company_profile`) are
blocked until the tenant is reconnected once. Disconnecting keeps the stored id.

## bexio API notes

Purchase bills use API 4.0 (`POST /4.0/purchase/bills`, `PUT /4.0/purchase/bills/{id}/bookings/{status}`).
The payload builder lives in `app/services/bexio.py` (`build_bill_payload`) and follows the OpenAPI spec embedded in docs.bexio.com; adjust field
names there if bexio rejects the first real call (the error is shown on the document page).
Manual entries use API 3.0 (`POST /3.0/accounting/manual_entries` + file attach on the line).

## MCP endpoint

`POST /mcp` speaks MCP over Streamable HTTP (JSON-RPC 2.0) so the tool can be driven from a chat client:
todo list, document details, booking with corrections, rules (single and bulk upsert), payment accounts,
tenant settings, master data refresh and the history import. Set `MCP_TOKEN` (bearer token, `openssl rand -hex 32`);
without it the endpoint is disabled. Writing tools only run with `confirm=true`, otherwise they return a preview.
Nothing is deleted; rules and payment accounts are deactivated instead.

## Payment files

`Zahlungen` lists the open purchase bills booked by the tool. Selected bills are written to a
pain.001.001.09 file (Swiss Payment Standard) for upload in e-banking; each tenant needs its debtor IBAN
and name in the settings. Bills in a file are marked so they are not paid twice; once bexio reports the
bill as paid (bank sync), the worker closes the ticket. QR-bills carry the QR reference, other invoices
the invoice number as message.
