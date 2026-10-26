"""Сервис кандидатов: регистрация и профиль."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import repositories as repo
from app.database.models import Candidate


async def register(
    db: AsyncSession,
    *,
    telegram_id: int,
    telegram_username: str | None,
    full_name: str | None = None,
    contact_data: str | None = None,
) -> Candidate:
    """Создаёт или обновляет кандидата (идемпотентно по telegram_id)."""
    return await repo.create_or_update_candidate(
        db,
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        full_name=full_name,
        contact_data=contact_data,
    )


async def set_contact(db: AsyncSession, candidate: Candidate, contact: str) -> None:
    candidate.contact_data = contact
    await db.flush()


async def set_name(db: AsyncSession, candidate: Candidate, name: str) -> None:
    candidate.full_name = name
    await db.flush()