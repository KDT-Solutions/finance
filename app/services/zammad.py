"""Zammad REST client: tickets per group, attachments, internal notes, group/state changes.
Never sends anything to customers: only internal notes, group and state updates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

PENDING_REMINDER = "pending reminder"


def parse_time(value) -> datetime | None:
    """Zammad timestamps ("2026-11-09T08:00:00.000Z") as aware datetime, None if empty or unreadable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Attachment:
    ticket_id: int
    article_id: int
    attachment_id: int
    filename: str
    size: int
    content_type: str


class ZammadClient:
    def __init__(self, url: str | None = None, token: str | None = None):
        s = get_settings()
        self.base = (url or s.zammad_url).rstrip("/")
        self.token = token or s.zammad_token
        self._client = httpx.Client(base_url=self.base, timeout=60, headers={"Authorization": f"Token token={self.token}"})

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params):
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict):
        r = self._client.put(path, json=body)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict):
        r = self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    # --- tickets ---------------------------------------------------------------
    def open_tickets_in_group(self, group: str, limit: int = 20) -> list[dict]:
        """Open/new tickets of a group, oldest first."""
        query = f'group.name:"{group}" AND (state.name:new OR state.name:open)'
        data = self._get("/api/v1/tickets/search", query=query, limit=limit, expand="true", sort_by="created_at", order_by="asc")
        if isinstance(data, dict) and "tickets" in data:  # older response shape
            assets = data.get("assets", {}).get("Ticket", {})
            return [assets[str(i)] for i in data["tickets"] if str(i) in assets]
        return data if isinstance(data, list) else []

    def ticket(self, ticket_id: int) -> dict:
        return self._get(f"/api/v1/tickets/{ticket_id}", expand="true")

    def articles(self, ticket_id: int) -> list[dict]:
        return self._get(f"/api/v1/ticket_articles/by_ticket/{ticket_id}")

    def pdf_attachments(self, ticket_id: int) -> list[Attachment]:
        out: list[Attachment] = []
        for art in self.articles(ticket_id):
            for att in art.get("attachments", []) or []:
                name = att.get("filename", "")
                ctype = (att.get("preferences") or {}).get("Content-Type", "") or ""
                if name.lower().endswith(".pdf") or ctype == "application/pdf":
                    out.append(Attachment(ticket_id, art["id"], att["id"], name, int(att.get("size") or 0), ctype))
        return out

    def download(self, att: Attachment) -> bytes:
        r = self._client.get(f"/api/v1/ticket_attachment/{att.ticket_id}/{att.article_id}/{att.attachment_id}")
        r.raise_for_status()
        return r.content

    def article_bodies(self, ticket_id: int) -> str:
        parts = []
        for art in self.articles(ticket_id):
            body = art.get("body") or ""
            if art.get("content_type") == "text/html":
                import re

                body = re.sub(r"<[^>]+>", " ", body)
            parts.append(body)
        return "\n".join(parts)[:6000]

    def agent_marked_paid(self, ticket_id: int) -> bool:
        """True if an agent (not the customer/sender) wrote 'bezahlt' into the ticket, e.g. as internal note,
        and it is not a negation ('nicht bezahlt', 'unbezahlt')."""
        import re

        for art in self.articles(ticket_id):
            if (art.get("sender") or "") != "Agent":
                continue
            body = art.get("body") or ""
            if art.get("content_type") == "text/html":
                body = re.sub(r"<[^>]+>", " ", body)
            text = body.lower()
            if text.startswith("finance:"):
                continue
            if re.search(r"\b(nicht|noch nicht|un)\s*bezahlt\b", text):
                continue
            if re.search(r"\bbezahlt\b", text):
                return True
        return False

    # --- writes (internal only) -------------------------------------------------
    def add_note(self, ticket_id: int, body: str) -> dict:
        return self._post("/api/v1/ticket_articles", {
            "ticket_id": ticket_id, "body": body, "type": "note", "internal": True, "content_type": "text/plain",
        })

    def set_group(self, ticket_id: int, group: str) -> dict:
        return self._put(f"/api/v1/tickets/{ticket_id}", {"group": group})

    def set_state(self, ticket_id: int, state: str) -> dict:
        return self._put(f"/api/v1/tickets/{ticket_id}", {"state": state})

    def set_pending(self, ticket_id: int, until: datetime) -> dict:
        """Ticket auf "pending reminder" (Warten auf Erinnerung) bis until."""
        return self._put(f"/api/v1/tickets/{ticket_id}", {"state": PENDING_REMINDER, "pending_time": until.astimezone(timezone.utc).isoformat()})

    def ticket_state(self, ticket_id: int) -> tuple[str, datetime | None]:
        """(Statusname, pending_time) des Tickets."""
        t = self.ticket(ticket_id)
        return str(t.get("state") or ""), parse_time(t.get("pending_time"))

    def groups(self) -> list[dict]:
        return self._get("/api/v1/groups")
