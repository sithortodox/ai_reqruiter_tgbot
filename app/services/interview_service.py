"""Диалоговый модуль: состояние интервью, последовательность вопросов.

Разделы 4.1–4.3, 6 TZ.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import repositories as repo
from app.database.models import (
    ALLOWED_TRANSITIONS,
    InterviewSession,
    SessionStatus,
    Vacancy,
)

logger = logging.getLogger(__name__)


class TransitionError(Exception):
    """Недопустимый переход состояния (защита целостности сессии).."""


def can_transition(current: str, target: str) -> bool:
    if current == target and target in ALLOWED_TRANSITIONS and target in (
        SessionStatus.COLLECTING_RESUME,
        SessionStatus.INTERVIEWING,
    ):
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition(record: InterviewSession, target: str) -> None:
    if not can_transition(record.status, target):
        raise TransitionError(
            f"Недопустимый переход {record.status} -> {target} (сессия {record.id})"
        )
    record.status = target
    record.last_activity_at = datetime.now(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Вспомогательные вопросы-промпты
# ---------------------------------------------------------------------------


def texts_for(vacancy: Vacancy) -> dict:
    """Тексты сообщений вакансии с дефолтами из демо-конфигурации."""
    from app.seed import DEMO_VACANCY

    defaults = DEMO_VACANCY["evaluation_config"]["texts"]
    custom = (vacancy.evaluation_config or {}).get("texts", {})
    return {**defaults, **custom}


def consent_text_for(vacancy: Vacancy, scenario_consent: str) -> str:
    """Настраиваемый текст уведомления о персональных данных (4.2)."""
    custom = (vacancy.evaluation_config or {}).get("texts", {}).get("consent")
    if custom:
        return custom
    if scenario_consent:
        return scenario_consent
    return texts_for(vacancy)["consent"]


# ---------------------------------------------------------------------------
# Запуск и возобновление
# ---------------------------------------------------------------------------


async def start_or_resume(
    db: AsyncSession,
    *,
    candidate: "repo.Candidate",
    vacancy: Vacancy,
    settings: Settings,
) -> tuple[InterviewSession, str, bool]:
    """Возвращает (сессия, сообщение, is_resumed).

    Защита от дублей: при наличии незавершённой сессии просто возобновляем её.
    """
    active = await repo.get_active_session(db, candidate.id)
    if active is not None:
        text = _resume_message(db_active=active, vacancy=vacancy)
        return active, text, True

    # Версии фиксируются на момент старта (раздел 4.8: правки не ломают идущие)
    scenario = await _current_scenario(db, vacancy)
    prompt_version = await _current_prompt_version(db, vacancy)

    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=scenario["version"],
        prompt_version=prompt_version,
        scenario_snapshot=scenario["questions"],
    )
    record.data_step = 0
    transition(record, SessionStatus.COLLECTING_DATA)

    consent = consent_text_for(vacancy, scenario.get("consent_text", ""))
    msg = f"{texts_for(vacancy)['greeting']}\n\n{consent}\n\n✅ Подтвердить — кнопкой ниже или ответом «да»."
    await repo.add_audit_event(
        db, "interview_started", session_id=record.id, vacancy=vacancy.slug
    )
    return record, msg, False


def _resume_message(*, db_active: InterviewSession, vacancy: Vacancy) -> str:
    t = texts_for(vacancy)
    status = db_active.status
    if status in (SessionStatus.COLLECTING_DATA, SessionStatus.COLLECTING_RESUME):
        return f"👋 Вы уже начали собеседование по вакансии «{vacancy.title}».\n" + t["resumed"]
    if status == SessionStatus.INTERVIEWING:
        return f"{t['resumed']}\nВопрос №{db_active.current_step + 1}:"
    if status == SessionStatus.ANALYZING:
        return t["analysis_running"]
    if status == SessionStatus.ANALYSIS_FAILED:
        return t["analysis_error"]
    if status == SessionStatus.MANUAL_REVIEW:
        return t["finished_manual_review"]
    return t["resumed"]


async def current_question(record: InterviewSession) -> dict | None:
    """Вопрос по текущему шагу из снимка сценария сессии."""
    questions = [
        q for q in (record.scenario_snapshot or []) if q.get("enabled", True)
    ]
    questions.sort(key=lambda q: q.get("order", 0))
    if record.current_step >= len(questions):
        return None
    return questions[record.current_step]


async def questions_of(record: InterviewSession) -> list[dict]:
    questions = [
        q for q in (record.scenario_snapshot or []) if q.get("enabled", True)
    ]
    questions.sort(key=lambda q: q.get("order", 0))
    return questions


# ---------------------------------------------------------------------------
# Шаги сбора данных
# ---------------------------------------------------------------------------


def validate_contact(text: str) -> bool:
    """Контакт: телефон, e-mail или @username — без сплошного мусора."""
    t = text.strip()
    if len(t) > 255 or len(t) < 3:
        return False
    digits = sum(c.isdigit() for c in t)
    if digits >= 7:
        return True
    if "@" in t and ("." in t or t.startswith("@")):
        return True
    return False


def validate_name(text: str) -> bool:
    t = text.strip()
    if not 2 <= len(t) <= 255:
        return False
    return any(c.isalpha() for c in t)


async def accept_consent(db: AsyncSession, record: InterviewSession) -> None:
    record.consent_given = True
    record.data_step = 1
    record.last_activity_at = _now()
    await repo.add_audit_event(db, "consent_given", session_id=record.id)


async def accept_name(db: AsyncSession, record: InterviewSession, name: str) -> None:
    candidate = await repo.get_candidate(db, record.candidate_id)
    if candidate is not None:
        candidate.full_name = name.strip()
        candidate.updated_at = _now()
    record.data_step = 2
    record.last_activity_at = _now()
    await repo.add_audit_event(
        db, "name_received", session_id=record.id, name=name.strip()
    )


async def accept_contact(db: AsyncSession, record: InterviewSession, contact: str) -> None:
    candidate = await repo.get_candidate(db, record.candidate_id)
    if candidate is not None:
        candidate.contact_data = contact.strip()
        candidate.updated_at = _now()
    record.last_activity_at = _now()
    transition(record, SessionStatus.COLLECTING_RESUME)
    await repo.add_audit_event(
        db, "contact_received", session_id=record.id, contact=contact.strip()
    )


# ---------------------------------------------------------------------------
# Ответы на вопросы
# ---------------------------------------------------------------------------


async def accept_answer(
    db: AsyncSession,
    record: InterviewSession,
    question: dict,
    answer_text: str,
    settings: Settings,
) -> tuple[bool, str | None]:
    """Сохраняет ответ и двигает шаг. Возвращает (ok, error_message)."""
    limit = int(question.get("max_length") or settings.max_message_length)
    answer_text = answer_text.strip()
    if not answer_text:
        return False, texts_for_empty(record)
    if len(answer_text) > limit:
        return False, f"Ответ слишком длинный (лимит {limit} символов). Сократите, пожалуйста."

    required = bool(question.get("required", False))
    if required and len(answer_text) < 2:
        return False, "Этот вопрос обязательный — пожалуйста, ответьте."

    saved = await repo.save_answer(
        db,
        session_id=record.id,
        question_id=str(question["id"]),
        question_text=str(question.get("text", "")),
        answer_text=answer_text,
    )
    record.last_activity_at = _now()
    if saved is None:
        # Повторное событие Telegram — ответ уже сохранён, просто идём дальше
        logger.info("Дубликат ответа пропущен session=%s q=%s", record.id, question["id"])

    questions = await questions_of(record)
    record.current_step += 1
    if record.current_step >= len(questions):
        transition(record, SessionStatus.ANALYZING)
        await repo.add_audit_event(
            db, "interview_completed", session_id=record.id, answers=len(questions)
        )
        return True, None
    return True, None


def texts_for_empty(record: InterviewSession) -> str:
    return "Ответ не может быть пустым. Пожалуйста, напишите ответ."


def next_question_text(record: InterviewSession, vacancy: Vacancy) -> str | None:
    questions = [q for q in (record.scenario_snapshot or []) if q.get("enabled", True)]
    questions.sort(key=lambda q: q.get("order", 0))
    if record.current_step >= len(questions):
        return None
    q = questions[record.current_step]
    prefix = f"Вопрос {record.current_step + 1}/{len(questions)}:\n"
    required = " *(обязательный)" if q.get("required") else ""
    return f"{prefix}{q['text']}{required}"


# ---------------------------------------------------------------------------
# Отмена / завершение
# ---------------------------------------------------------------------------


async def cancel_session(
    db: AsyncSession, record: InterviewSession, vacancy: Vacancy
) -> str:
    transition(record, SessionStatus.CANCELLED)
    record.completed_at = _now()
    await repo.add_audit_event(db, "session_cancelled", session_id=record.id)
    return texts_for(vacancy)["cancelled"]


async def expire_session(
    db: AsyncSession, record: InterviewSession, vacancy: Vacancy
) -> str | None:
    """Просрочка по тайм-ауту. None — если состояние не подлежит истечению."""
    if record.status in (
        SessionStatus.COMPLETED,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    ):
        return None
    transition(record, SessionStatus.EXPIRED)
    record.completed_at = _now()
    await repo.add_audit_event(db, "session_expired", session_id=record.id)
    return texts_for(vacancy)["expired"]


# ---------------------------------------------------------------------------
# Вспомогательные загрузки версий
# ---------------------------------------------------------------------------


async def _current_scenario(db: AsyncSession, vacancy: Vacancy) -> dict:
    """Снимок вопросов текущей версии сценария вакансии."""
    if vacancy.current_scenario_version_id:
        from app.database.models import ScenarioVersion

        sv = await db.get(ScenarioVersion, vacancy.current_scenario_version_id)
        if sv is not None:
            return {"version": sv.version, "questions": sv.questions,
                    "consent_text": sv.consent_text}
    # Fallback: вопросы из evaluation_config (если админ задал их напрямую)
    return {
        "version": 1,
        "questions": (vacancy.evaluation_config or {}).get("questions", []),
        "consent_text": "",
    }


async def _current_prompt_version(db: AsyncSession, vacancy: Vacancy) -> int:
    if vacancy.current_prompt_version_id:
        from app.database.models import PromptVersion

        pv = await db.get(PromptVersion, vacancy.current_prompt_version_id)
        if pv is not None:
            return pv.version
    return 1


async def get_scenario(db: AsyncSession, vacancy: Vacancy) -> dict:
    return await _current_scenario(db, vacancy)


async def get_prompt(db: AsyncSession, vacancy: Vacancy) -> tuple[int, str]:
    if vacancy.current_prompt_version_id:
        from app.database.models import PromptVersion

        pv = await db.get(PromptVersion, vacancy.current_prompt_version_id)
        if pv is not None:
            return pv.version, pv.system_prompt
    from app.seed import load_default_prompt

    return 1, load_default_prompt()