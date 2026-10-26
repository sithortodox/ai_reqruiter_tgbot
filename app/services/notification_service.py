"""Модуль уведомлений: карточка кандидата в чат рекрутеров (раздел 4.8 TZ).

Сообщения идут через outbox с idempotency_key — одна карточка отправляется
ровно один раз, даже при повторной обработке события.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.database import repositories as repo
from app.database.models import InterviewSession, Vacancy

logger = logging.getLogger(__name__)

# Лимит Telegram Bot API на длину текста сообщения
TELEGRAM_MAX_LEN = 4096
# ПД не выводим в уведомление сверх необходимого (4.8 TZ)
SENSITIVE_SKIP = ("telegram_id", "password", "inn", "снилс")


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _section(title: str, items: list | None) -> str:
    if not items:
        return ""
    lines = "\n".join(f"• {_esc(i)}" for i in items[:15])
    return f"\n\n<b>{title}</b>\n{lines}"


def build_candidate_card(
    *,
    record: InterviewSession,
    vacancy: Vacancy,
    candidate_name: str | None,
    contact: str | None,
    report: dict,
    routing_status: str,
    answers: list[tuple[str, str]],
    score: int | None,
) -> list[str]:
    """Формирует карточку кандидата; при переполнении разбивает на части."""
    badge = {
        "recommended": "🟢 <b>ВЫСОКИЙ ПРИОРИТЕТ — RECOMMENDED</b>",
        "not_recommended": "⚪️ NOT_RECOMMENDED (архив)",
        "manual_review": "🟡 <b>ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА</b>",
    }.get(routing_status, _esc(routing_status))

    started = record.started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    date_str = started.strftime("%d.%m.%Y %H:%M UTC") if started else "—"

    header = [
        f"{badge}",
        "",
        f"<b>Кандидат:</b> {_esc(candidate_name or 'не указано')}",
        f"<b>Контакт:</b> {_esc(contact or 'не указан')}",
        f"<b>Вакансия:</b> {_esc(vacancy.title)}",
        f"<b>Дата интервью:</b> {date_str}",
        f"<b>Оценка:</b> {score if score is not None else '—'}/10",
        f"<b>Статус:</b> {_esc(routing_status)}",
        f"<b>ID сессии:</b> <code>{_esc(record.id)}</code>",
    ]

    body_parts: list[str] = []
    summary = report.get("summary")
    if summary:
        body_parts.append(f"\n<b>Резюме:</b> {_esc(_truncate(str(summary), 800))}")

    skills = report.get("skills") or []
    if skills:
        lines = []
        for s in skills[:10]:
            if not isinstance(s, dict):
                continue
            name = _esc(s.get("name", ""))
            level = _esc(s.get("level", ""))
            evidence = _esc(_truncate(str(s.get("evidence", "")), 200))
            lines.append(f"• {name} ({level}) — {evidence}")
        if lines:
            body_parts.append("\n<b>Подтверждённые навыки:</b>\n" + "\n".join(lines))

    body_parts.append(_section("Сильные стороны", report.get("strengths")))
    body_parts.append(_section("Пробелы", report.get("gaps")))
    body_parts.append(_section("Несоответствия обязательным", report.get("mandatory_unmet")))
    body_parts.append(_section("Стоп-факторы", report.get("stop_factors")))
    body_parts.append(_section("Требует проверки", report.get("facts_to_verify")))

    reasoning = report.get("reasoning_summary")
    if reasoning:
        body_parts.append(
            f"\n<b>Обоснование:</b> {_esc(_truncate(str(reasoning), 900))}"
        )
    recommendation = report.get("recommendation")
    confidence = report.get("confidence")
    if recommendation:
        body_parts.append(
            f"\n<b>Рекомендация AI:</b> {_esc(recommendation)} "
            f"(уверенность: {_esc(confidence or '—')})"
        )

    answers_text = ""
    if answers:
        chunks = []
        for q, a in answers:
            chunks.append(f"<b>В:</b> {_esc(_truncate(q, 200))}\n<i>О:</i> {_esc(_truncate(a, 400))}")
        answers_text = "\n\n<b>Ответы:</b>\n" + "\n\n".join(chunks)

    # Собираем сообщения, не превышая лимит
    messages: list[str] = []
    current = "\n".join(header)
    for part in body_parts:
        if not part:
            continue
        if len(current) + len(part) > TELEGRAM_MAX_LEN - 200:
            messages.append(current)
            current = part.lstrip("\n")
        else:
            current += part
    # Ответы — отдельным блоком/сообщением (лимиты Telegram)
    if answers_text:
        remaining = TELEGRAM_MAX_LEN - len(current) - 10
        if len(answers_text) <= remaining:
            current += answers_text
        else:
            messages.append(current)
            current = answers_text
    messages.append(current)

    # Разбиваем слишком длинные куски (страховка)
    final: list[str] = []
    for msg in messages:
        while len(msg) > TELEGRAM_MAX_LEN:
            final.append(msg[:TELEGRAM_MAX_LEN])
            msg = msg[TELEGRAM_MAX_LEN:]
        if msg:
            final.append(msg)
    return final


async def enqueue_recruiter_card(
    db: AsyncSession,
    *,
    record: InterviewSession,
    vacancy: Vacancy,
    candidate_name: str | None,
    contact: str | None,
    report: dict,
    routing_status: str,
    answers: list[tuple[str, str]],
    score: int | None,
    settings: Settings,
    priority: int = 0,
) -> int:
    """Ставит карточку в outbox. Возвращает число созданных сообщений."""
    chat_id = settings.recruiters_chat_id_value
    if chat_id is None:
        logger.warning("RECRUITERS_CHAT_ID не задан — карточка не поставлена в очередь")
        return 0

    parts = build_candidate_card(
        record=record,
        vacancy=vacancy,
        candidate_name=candidate_name,
        contact=contact,
        report=report,
        routing_status=routing_status,
        answers=answers,
        score=score,
    )
    created = 0
    for idx, text in enumerate(parts):
        key = f"card:{record.id}:{idx}"
        msg = await repo.enqueue_message(
            db,
            idempotency_key=key,
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            priority=priority,
        )
        if msg is not None:
            created += 1
    return created


async def enqueue_candidate_message(
    db: AsyncSession,
    *,
    record: InterviewSession,
    telegram_chat_id: int,
    text: str,
    key_suffix: str = "final",
) -> bool:
    """Ставит личное сообщение кандидату в outbox (идемпотентно)."""
    key = f"dm:{record.id}:{key_suffix}"
    msg = await repo.enqueue_message(
        db,
        idempotency_key=key,
        chat_id=telegram_chat_id,
        text=text,
        parse_mode="HTML",
        priority=1,
    )
    return msg is not None