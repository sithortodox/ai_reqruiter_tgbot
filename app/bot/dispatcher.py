"""Сборка диспетчера aiogram: роутеры, middleware, внедрение зависимостей."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import handlers
from app.bot.middlewares import (
    DbSessionMiddleware,
    IdempotencyMiddleware,
    ThrottleMiddleware,
)
from app.config import Settings
from app.database.base import get_session_factory


def create_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings

    # Порядок: идемпотентность -> throttle -> сессия БД
    dp.update.outer_middleware(IdempotencyMiddleware())
    dp.update.outer_middleware(ThrottleMiddleware(min_interval=0.7))
    dp.update.outer_middleware(DbSessionMiddleware(get_session_factory()))

    dp.include_router(handlers.router)
    return dp


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def on_startup_hook(bot: Bot) -> None:  # pragma: no cover - заглушка хуков
    """Действия при старте (при необходимости)."""
    return None