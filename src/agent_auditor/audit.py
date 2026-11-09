from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import AuditEvent


def record(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str,
    detail: dict[str, Any] | None = None,
    actor: str = "api",
) -> AuditEvent:
    """Append an audit event to the current transaction (caller commits)."""
    event = AuditEvent(
        actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, detail=detail or {}
    )
    db.add(event)
    return event
