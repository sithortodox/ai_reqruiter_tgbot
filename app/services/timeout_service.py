"""Воркер тайм-аутов: истечение неактивных сессий и напоминания (4.3 TZ)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.database.base import session_scope
from app.database import repositories as repo
from app.database.models import SessionStatus, Vacancy
from app.services import interview_service, notification_service
from app.services.dispatch_service import flush_outbox

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60.0

ACTIVE_STATUSES = (
    SessionStatus.COLLECTING_DATA,
    SessionStatus.COLLECTING_RESUME,
    SessionStatus.INTERVIEWING,
)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def sweep_timeouts(settings: Settings, bot) -> tuple[int, int]:
    """Помечает истёкшие сессии и шлёт напоминания. Возвращает (expired, reminders)."""
    expired = reminders = 0
    now = datetime.now(timezone.utc)
    timeout = timedelta(minutes=settings.interview_timeout_minutes)
    reminder_delta = timedelta(minutes=settings.interview_reminder_minutes)

    async with session_scope() as db:
        sessions = await repo.list_timeout_candidates(db)  # type: ignore[attr-defined]
        for record in sessions:
            vacancy = await repo.get_vacancy(db, record.vacancy_id)
            if vacancy is None:
                continue
            eval_cfg = vacancy.evaluation_config or {}
            session_timeout = timedelta(
                minutes=int(eval_cfg.get("timeout_minutes", settings.interview_timeout_minutes))
            )
            last = _as_utc(record.last_activity_at) or _as_utc(record.started_at)
            if last is None:
                continue
            if now - last >= session_timeout:
                text = await interview_service.expire_session(db, record, vacancy)
                expired += 1
                if text:
                    candidate = await repo.get_candidate(db, record.candidate_id)
                    if candidate is not None:
                        await notification_service.enqueue_candidate_message(
                            db,
                            record=record,
                            telegram_chat_id=candidate.telegram_id,
                            text=text,
                            key_suffix="expired",
                        )
                continue
            # Напоминание до истечения
            reminder_minutes = int(
                eval_cfg.get("reminder_minutes", settings.interview_reminder_minutes)
            )
            reminded = _as_utc(record.reminded_at)
            if (
                reminded is None
                and now - last >= reminder_delta
                and reminder_minutes > 0
                and now - last < session_timeout
            ):
                texts = interview_service.texts_for(vacancy)
                candidate = await repo.get_candidate(db, record.candidate_id)
                if candidate is not None:
                    await notification_service.enqueue_candidate_message(
                        db,
                        record=record,
                        telegram_chat_id=candidate.telegram_id,
                        text=texts.get("reminder", "Напоминаем: интервью не завершено."),
                        key_suffix="reminder",
                    )
                    record.reminded_at = now
                    reminders += 1

    if expired or reminders:
        await flush_outbox(bot, settings)
    return expired, reminders


async def timeout_worker(settings: Settings, bot) -> None:
    logger.info("Воркер тайм-аутов запущен (проверка каждые %ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            expired, reminders = await sweep_timeouts(settings, bot)
            if expired or reminders:
                logger.info(
                    "Тайм-ауты: истекло %s, напоминаний %s", expired, reminders
                )
        except asyncio.CancelledError:
            logger.info("Воркер тайм-аутов остановлен")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка цикла тайм-аутов")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)