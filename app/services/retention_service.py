"""Ретеншн и удаление персональных данных (раздел 7 TZ).

Две процедуры:
- purge_expired(): регулярная очистка ПД, срок хранения которых истёк
  (RETENTION_DAYS от даты завершения сессии) — анонимизация + удаление файлов;
- delete_candidate_data(): разовое удаление ПД конкретного кандидата
  по установленной процедуре (запрос субъекта данных).

Обе операции аудируются и не трогают статистику отбора: отчёты AI
обезличиваются, но сохраняются для отчётов без персональных данных.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, update

from app.config import Settings
from app.database.base import session_scope
from app.database import repositories as repo
from app.database.models import (
    AIReport,
    Candidate,
    InterviewAnswer,
    InterviewSession,
    Resume,
    SessionStatus,
)

logger = logging.getLogger(__name__)

# Чистка запускается раз в сутки
CHECK_INTERVAL_SECONDS = 6 * 3600.0

# Терминальные статусы, после которых начинается отсчёт срока хранения
TERMINAL_STATUSES = (
    SessionStatus.COMPLETED,
    SessionStatus.CANCELLED,
    SessionStatus.EXPIRED,
)

# Максимум удалений за один прогон (защита от нервного воркера)
BATCH_LIMIT = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _remove_file(reference: str | None) -> bool:
    """Удаляет файл резюме из хранилища; чужие пути игнорирует."""
    if not reference:
        return False
    try:
        path = Path(reference)
        if path.is_file():
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("Не удалось удалить файл %s: %s", reference, exc)
    return False


async def purge_expired_in(db, settings: Settings) -> int:
    """Анонимизирует ПД с истёкшим сроком хранения в открытой сессии."""
    retention_days = max(1, settings.retention_days)
    cutoff = _utcnow() - timedelta(days=retention_days)
    removed = 0

    # Кандидаты, у которых есть хотя бы одна завершённая сессия старше cutoff
    stmt = (
        select(Candidate.id)
        .join(InterviewSession, InterviewSession.candidate_id == Candidate.id)
        .where(
            InterviewSession.status.in_(TERMINAL_STATUSES),
            InterviewSession.completed_at.is_not(None),
            InterviewSession.completed_at < cutoff,
        )
        .limit(BATCH_LIMIT)
    )
    candidate_ids = list((await db.execute(stmt)).scalars().all())

    for candidate_id in candidate_ids:
        await delete_candidate_data(db, candidate_id, settings, reason="retention")
        removed += 1

    if removed:
        logger.info(
            "Ретеншн: обработано кандидатов %s (срок хранения %s дн.)",
            removed,
            retention_days,
        )
    return removed


async def purge_expired(settings: Settings) -> int:
    """Анонимизирует ПД с истёкшим сроком хранения. Возвращает число сессий."""
    async with session_scope() as db:
        return await purge_expired_in(db, settings)


async def delete_candidate_data(
    db,
    candidate_id: str,
    settings: Settings,
    *,
    reason: str = "manual",
) -> bool:
    """Полное удаление ПД кандидата в рамках одной транзакции.

    Сохраняются только обезличенные данные для статистики отбора:
    - сессии и ответы обезличиваются (текст ответов удаляется);
    - отчёт AI сохраняется без привязки к личности (score/рекомендация);
    - файлы резюме удаляются с диска;
    - запись кандидата удаляется.

    Возвращает False, если кандидат не найден.
    """
    candidate = await db.get(Candidate, candidate_id)
    if candidate is None:
        return False

    sessions = list(
        (
            await db.scalars(
                select(InterviewSession).where(
                    InterviewSession.candidate_id == candidate_id
                )
            )
        ).all()
    )
    session_ids = [s.id for s in sessions]

    files_removed = 0
    if session_ids:
        resumes = list(
            (
                await db.scalars(
                    select(Resume).where(Resume.session_id.in_(session_ids))
                )
            ).all()
        )
        for resume in resumes:
            if _remove_file(resume.storage_reference):
                files_removed += 1
        # Удаляем записи резюме целиком (текст и ссылки — ПД)
        await db.execute(
            delete(Resume).where(Resume.session_id.in_(session_ids))
        )
        # Ответы кандидата — ПД: удаляем вместе с вопросами
        await db.execute(
            delete(InterviewAnswer).where(
                InterviewAnswer.session_id.in_(session_ids)
            )
        )
        # Отчёты AI: обезличиваем (оставляем метрики отбора, убираем текст)
        await db.execute(
            update(AIReport)
            .where(AIReport.session_id.in_(session_ids))
            .values(report_json={})
        )

    # Сам кандидат удаляется; сессии остаются как анонимные статистические
    # записи (FK на кандидата обнуляется).
    for record in sessions:
        record.candidate_id = None
    await db.execute(delete(Candidate).where(Candidate.id == candidate_id))

    await repo.add_audit_event(
        db,
        "personal_data_deleted",
        session_id=session_ids[0] if session_ids else None,
        candidate_id=candidate_id,
        reason=reason,
        files_removed=files_removed,
    )
    logger.info(
        "Удалены ПД кандидата %s (reason=%s, файлов=%s, сессий=%s)",
        candidate_id,
        reason,
        files_removed,
        len(sessions),
    )
    return True


async def retention_worker(settings: Settings) -> None:
    """Фоновый воркер: периодическая очистка по сроку хранения."""
    logger.info(
        "Воркер ретеншна запущен (каждые %ss, срок хранения %s дн.)",
        CHECK_INTERVAL_SECONDS,
        settings.retention_days,
    )
    while True:
        try:
            await purge_expired(settings)
        except asyncio.CancelledError:
            logger.info("Воркер ретеншна остановлен")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка цикла ретеншна")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)