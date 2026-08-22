from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one immutable audit event inside the caller's transaction."""
    row = AuditLog(
        chat_id=chat_id,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row
