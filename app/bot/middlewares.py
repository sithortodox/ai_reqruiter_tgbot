"""Middleware: сессия БД, ограничение частоты, защита от спама."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class DbSessionMiddleware:
    """Отдаёт каждому апдейту свежую сессию БД с commit/rollback."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["db"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class ThrottleMiddleware(BaseMiddleware):
    """Ограничение частоты сообщений от одного пользователя (анти-спам)."""

    def __init__(self, min_interval: float = 0.7):
        self.min_interval = min_interval
        self._last: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        if now - self._last[user.id] < self.min_interval:
            logger.debug("Сообщение от user=%s отброшено (throttle)", user.id)
            return None
        self._last[user.id] = now
        # Очистка старых записей, чтобы не росла память
        if len(self._last) > 10_000:
            cutoff = now - 60
            for uid in [k for k, v in self._last.items() if v < cutoff]:
                del self._last[uid]
        return await handler(event, data)


class IdempotencyMiddleware(BaseMiddleware):
    """Защита от повторной обработки одного update_id (раздел 6 TZ).

    Telegram может доставить апдейт повторно — повторное событие не должно
    дублировать ответ или отчёт. Дополняется защитой на уровне БД:
    уникальные ключи ответов и outbox-сообщений.
    """

    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self._seen: dict[int, None] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = data.get("update")
        update_id = getattr(update, "update_id", None)
        if update_id is None:
            return await handler(event, data)
        if update_id in self._seen:
            logger.info("Повторный update_id=%s проигнорирован", update_id)
            return None
        self._seen[update_id] = None
        if len(self._seen) > self.capacity:
            # Вытесняем самую старую треть записей
            for key in list(self._seen)[: self.capacity // 3]:
                del self._seen[key]
        return await handler(event, data)