from sqlalchemy.ext.asyncio import AsyncSession

from groupbot.models import AuditLog


async def write_audit(
    session: AsyncSession,
    event_type: str,
    *,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
        )
    )
