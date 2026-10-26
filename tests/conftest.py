"""Общие фикстуры: тестовая SQLite-БД, настройки, чистое состояние приложения."""

from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Настройки тестового окружения (без .env и внешних сервисов)."""
    return Settings(
        _env_file=None,
        BOT_TOKEN="123:test-token",
        ADMIN_IDS="111",
        RECRUITERS_CHAT_ID="-100500",
        LLM_API_KEY="",  # демо-режим адаптера: без сети
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        STORAGE_DIR=str(tmp_path / "storage"),
    )


@pytest.fixture
async def db(settings, tmp_path):
    """Фикстура: чистая SQLite-БД с полной схемой на каждый тест."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.database.base import Base, reset_database_state
    from app.database import models  # noqa: F401 - регистрация моделей

    reset_database_state()
    engine = create_async_engine(settings.database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()
    reset_database_state()


@pytest.fixture
def vacancy_factory(db):
    """Создание вакансии с настройкой оценки."""
    from app.database import repositories as repo

    async def _make(threshold: int = 7, **overrides) -> "object":
        fields = dict(
            slug=f"vac-{id(overrides)}",
            title="Тестовая вакансия",
            description="описание",
            mandatory_requirements=["Python"],
            preferred_skills=["SQL"],
            evaluation_config={"score_threshold": threshold},
            active=True,
        )
        fields.update(overrides)
        return await repo.create_vacancy(db, **fields)

    return _make