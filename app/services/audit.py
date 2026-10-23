from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditLog


def log(db: Session, action: str, *, tenant_id: int | None = None, actor: str = "system",
        entity: str | None = None, entity_id: int | None = None, details: dict | None = None) -> None:
    db.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action, entity=entity, entity_id=entity_id, details=details))
