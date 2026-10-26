"""Диспетчер outbox: доставка очереди уведомлений в Telegram.

Запускается фоновой задачей; повторные попытки при недоступности Telegram API.
Защита от дублей — через уникальный idempotency_key в БД.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.config import Settings
from app.database.base import session_scope
from app.database import repositories as repo

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 3.0
MAX_ATTEMPTS = 5


async def flush_outbox(bot: "Bot", settings: Settings) -> tuple[int, int]:
    """Отправляет накопленные сообщения. Возвращает (sent, failed)."""
    sent = failed = 0
    async with session_scope() as db:
        messages = await repo.pending_messages(db, limit=20)
        for message in messages:
            if message.attempts >= MAX_ATTEMPTS:
                await repo.mark_message_failed(db, message, "превышен лимит попыток")
                failed += 1
                continue
            try:
                await bot.send_message(
                    chat_id=message.chat_id,
                    text=message.text,
                    parse_mode=message.parse_mode,
                    disable_web_page_preview=True,
                )
                await repo.mark_message_sent(db, message)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                # Telegram временно недоступна или чат закрыт — повтор позже
                await repo.mark_message_failed(db, message, str(exc))
                failed += 1
                logger.warning(
                    "Не удалось отправить сообщение id=%s: %s", message.id, exc
                )
    return sent, failed


async def outbox_worker(bot: "Bot", settings: Settings) -> None:
    """Фоновый цикл доставки; останавливается по отмене задачи."""
    logger.info("Outbox-воркер запущен (интервал %ss)", FLUSH_INTERVAL_SECONDS)
    while True:
        try:
            await flush_outbox(bot, settings)
        except asyncio.CancelledError:
            logger.info("Outbox-воркер остановлен")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка цикла outbox — повтор через %ss", FLUSH_INTERVAL_SECONDS)
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)