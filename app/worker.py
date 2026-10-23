"""Background loop: Zammad ingest -> extraction/decision -> auto booking -> paid check -> daily summary."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import healthcheck
from .config import get_settings
from .db import SessionLocal, init_db
from .models import Document, Tenant
from .services import notify, pipeline
from .services.zammad import ZammadClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

_last_paid_check = 0.0
_last_summary_day = ""


def tick() -> None:
    global _last_paid_check, _last_summary_day
    s = get_settings()
    if not (s.zammad_url and s.zammad_token):
        log.warning("Zammad nicht konfiguriert, Worker wartet")
        return
    z = ZammadClient()
    try:
        with SessionLocal() as db:
            tenants = db.query(Tenant).filter(Tenant.active.is_(True)).all()
            for t in tenants:
                n = pipeline.ingest_tenant(db, z, t)
                if n:
                    log.info("%s: %d neue Belege", t.code, n)
            for doc in db.query(Document).filter(Document.state == "new").order_by(Document.id).limit(20).all():
                if not doc.tenant.bexio_connected and not s.anthropic_api_key:
                    continue
                log.info("verarbeite Beleg %s (%s)", doc.id, doc.filename)
                pipeline.process_document(db, z, doc)
            if time.time() - _last_paid_check > 6 * 3600:
                _last_paid_check = time.time()
                for t in tenants:
                    if t.bexio_connected:
                        try:
                            closed = pipeline.check_paid(db, z, t)
                            if closed:
                                log.info("%s: %d bezahlte Rechnungen, Tickets geschlossen", t.code, closed)
                        except Exception:  # noqa: BLE001
                            log.exception("check_paid failed for %s", t.code)
            local = datetime.now(ZoneInfo(s.tz))
            today = local.date().isoformat()
            if local.hour >= s.summary_hour and _last_summary_day != today:
                _last_summary_day = today
                text = pipeline.summary_text(db)
                if text:
                    notify.pushover("finance: offene Belege", f"Todo {text}\n{s.public_base_url}")
    finally:
        z.close()


def main() -> None:
    init_db()
    s = get_settings()
    log.info("worker gestartet, Intervall %ss", s.worker_interval_seconds)
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        healthcheck.beat()
        time.sleep(s.worker_interval_seconds)


if __name__ == "__main__":
    main()
