"""Репозитории: вся работа с БД через сессии SQLAlchemy (без SQL в хендлерах)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AIReport,
    AuditLog,
    Candidate,
    DeliveryStatus,
    InterviewAnswer,
    InterviewSession,
    OutgoingMessage,
    PromptVersion,
    Resume,
    ScenarioVersion,
    SessionStatus,
    Setting,
    Vacancy,
    gen_uuid,
)


async def create_candidate(
    session: AsyncSession,
    telegram_id: int,
    telegram_username: str | None,
    full_name: str | None = None,
    contact_data: str | None = None,
) -> Candidate:
    """Создание кандидата (для тестирования и администрирования)."""
    candidate = Candidate(
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        full_name=full_name,
        contact_data=contact_data,
    )
    session.add(candidate)
    await session.flush()
    return candidate


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


async def get_candidate_by_telegram(
    session: AsyncSession, telegram_id: int
) -> Candidate | None:
    return await session.scalar(
        select(Candidate).where(Candidate.telegram_id == telegram_id)
    )


async def get_candidate(session: AsyncSession, candidate_id: str) -> Candidate | None:
    return await session.get(Candidate, candidate_id)


async def create_or_update_candidate(
    session: AsyncSession,
    *,
    telegram_id: int,
    telegram_username: str | None,
    full_name: str | None = None,
    contact_data: str | None = None,
) -> Candidate:
    candidate = await get_candidate_by_telegram(session, telegram_id)
    if candidate is None:
        candidate = Candidate(
            telegram_id=telegram_id, telegram_username=telegram_username
        )
        session.add(candidate)
        await session.flush()
    else:
        if telegram_username:
            candidate.telegram_username = telegram_username
        if full_name:
            candidate.full_name = full_name
        if contact_data:
            candidate.contact_data = contact_data
        candidate.updated_at = utcnow()
        await session.flush()
    return candidate


# ---------------------------------------------------------------------------
# Vacancies / версии
# ---------------------------------------------------------------------------


async def list_vacancies(session: AsyncSession) -> list[Vacancy]:
    result = await session.scalars(
        select(Vacancy).order_by(Vacancy.created_at.asc())
    )
    return list(result)


async def list_active_vacancies(session: AsyncSession) -> list[Vacancy]:
    result = await session.scalars(
        select(Vacancy)
        .where(Vacancy.active.is_(True))
        .order_by(Vacancy.created_at.asc())
    )
    return list(result)


async def get_vacancy(session: AsyncSession, vacancy_id: str) -> Vacancy | None:
    return await session.get(Vacancy, vacancy_id)


async def get_vacancy_by_slug(session: AsyncSession, slug: str) -> Vacancy | None:
    return await session.scalar(select(Vacancy).where(Vacancy.slug == slug))


async def create_vacancy(session: AsyncSession, **fields) -> Vacancy:
    vacancy = Vacancy(**fields)
    session.add(vacancy)
    await session.flush()
    return vacancy


async def create_scenario_version(
    session: AsyncSession, vacancy_id: str, questions: list, consent_text: str = ""
) -> ScenarioVersion:
    last = await session.scalar(
        select(ScenarioVersion)
        .where(ScenarioVersion.vacancy_id == vacancy_id)
        .order_by(ScenarioVersion.version.desc())
        .limit(1)
    )
    version = ScenarioVersion(
        vacancy_id=vacancy_id,
        version=(last.version + 1) if last else 1,
        questions=questions,
        consent_text=consent_text,
    )
    session.add(version)
    await session.flush()
    return version


async def create_prompt_version(
    session: AsyncSession, vacancy_id: str, system_prompt: str
) -> PromptVersion:
    last = await session.scalar(
        select(PromptVersion)
        .where(PromptVersion.vacancy_id == vacancy_id)
        .order_by(PromptVersion.version.desc())
        .limit(1)
    )
    version = PromptVersion(
        vacancy_id=vacancy_id,
        version=(last.version + 1) if last else 1,
        system_prompt=system_prompt,
    )
    session.add(version)
    await session.flush()
    return version


async def get_scenario_version(
    session: AsyncSession, vacancy_id: str, version: int
) -> ScenarioVersion | None:
    return await session.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.vacancy_id == vacancy_id,
            ScenarioVersion.version == version,
        )
    )


async def get_prompt_version(
    session: AsyncSession, vacancy_id: str, version: int
) -> PromptVersion | None:
    return await session.scalar(
        select(PromptVersion).where(
            PromptVersion.vacancy_id == vacancy_id,
            PromptVersion.version == version,
        )
    )


# ---------------------------------------------------------------------------
# Сессии интервью
# ---------------------------------------------------------------------------


async def get_active_session(
    session: AsyncSession, candidate_id: str
) -> InterviewSession | None:
    """Незавершённая сессия кандидата (кроме терминальных статусов)."""
    terminal = [
        SessionStatus.COMPLETED,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    ]
    result = await session.scalars(
        select(InterviewSession)
        .where(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.not_in(terminal),
        )
        .order_by(InterviewSession.started_at.desc())
    )
    return result.first()


async def get_session_record(
    session: AsyncSession, session_id: str
) -> InterviewSession | None:
    return await session.get(InterviewSession, session_id)


async def create_session(
    session: AsyncSession,
    *,
    candidate_id: str,
    vacancy_id: str,
    scenario_version: int,
    prompt_version: int,
    scenario_snapshot: list,
) -> InterviewSession:
    record = InterviewSession(
        candidate_id=candidate_id,
        vacancy_id=vacancy_id,
        status=SessionStatus.NEW,
        scenario_version=scenario_version,
        prompt_version=prompt_version,
        scenario_snapshot=scenario_snapshot,
        started_at=utcnow(),
        last_activity_at=utcnow(),
    )
    session.add(record)
    await session.flush()
    return record


async def save_answer(
    session: AsyncSession,
    *,
    session_id: str,
    question_id: str,
    question_text: str,
    answer_text: str,
) -> InterviewAnswer | None:
    """Сохраняет ответ с идемпотентностью по (session_id, question_id)."""
    existing = await session.scalar(
        select(InterviewAnswer).where(
            InterviewAnswer.session_id == session_id,
            InterviewAnswer.question_id == question_id,
        )
    )
    if existing is not None:
        return None
    answer = InterviewAnswer(
        session_id=session_id,
        question_id=question_id,
        question_text=question_text,
        answer_text=answer_text,
    )
    session.add(answer)
    await session.flush()
    return answer


async def list_answers(session: AsyncSession, session_id: str) -> list[InterviewAnswer]:
    result = await session.scalars(
        select(InterviewAnswer)
        .where(InterviewAnswer.session_id == session_id)
        .order_by(InterviewAnswer.created_at.asc())
    )
    return list(result)


async def save_resume(
    session: AsyncSession,
    *,
    candidate_id: str,
    session_id: str | None,
    file_name: str | None,
    mime_type: str | None,
    storage_reference: str | None,
    extracted_text: str,
) -> Resume:
    record = Resume(
        candidate_id=candidate_id,
        session_id=session_id,
        file_name=file_name,
        mime_type=mime_type,
        storage_reference=storage_reference,
        extracted_text=extracted_text,
        char_count=len(extracted_text),
    )
    session.add(record)
    await session.flush()
    return record


async def latest_resume_for_session(
    session: AsyncSession, session_id: str
) -> Resume | None:
    result = await session.scalars(
        select(Resume)
        .where(Resume.session_id == session_id)
        .order_by(Resume.created_at.desc())
        .limit(1)
    )
    return result.first()


async def save_report(
    session: AsyncSession,
    *,
    session_id: str,
    score: int | None,
    recommendation: str | None,
    report_json: dict,
    provider: str | None,
    model_name: str | None,
    prompt_version: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
    duration_ms: int | None,
    attempts: int,
    error_code: str | None,
    analysis_status: str,
) -> AIReport:
    record = AIReport(
        session_id=session_id,
        score=score,
        recommendation=recommendation,
        report_json=report_json,
        provider=provider,
        model_name=model_name,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        attempts=attempts,
        error_code=error_code,
        analysis_status=analysis_status,
    )
    session.add(record)
    await session.flush()
    return record


async def latest_report_for_session(
    session: AsyncSession, session_id: str
) -> AIReport | None:
    result = await session.scalars(
        select(AIReport)
        .where(AIReport.session_id == session_id)
        .order_by(AIReport.created_at.desc())
        .limit(1)
    )
    return result.first()


# ---------------------------------------------------------------------------
# Журнал событий
# ---------------------------------------------------------------------------


async def add_audit_event(
    session: AsyncSession,
    event_type: str,
    *,
    session_id: str | None = None,
    **data,
) -> None:
    session.add(
        AuditLog(
            session_id=session_id,
            event_type=event_type,
            event_data={"event": event_type, **data},
        )
    )


# ---------------------------------------------------------------------------
# Outbox уведомлений
# ---------------------------------------------------------------------------


async def enqueue_message(
    session: AsyncSession,
    *,
    idempotency_key: str,
    chat_id: int,
    text: str,
    parse_mode: str | None = "HTML",
    priority: int = 0,
) -> OutgoingMessage | None:
    """Ставит сообщение в очередь; None, если такой ключ уже был (дубль)."""
    existing = await session.scalar(
        select(OutgoingMessage).where(
            OutgoingMessage.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return None
    record = OutgoingMessage(
        idempotency_key=idempotency_key,
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        priority=priority,
    )
    session.add(record)
    await session.flush()
    return record


async def pending_messages(session: AsyncSession, limit: int = 20) -> list[OutgoingMessage]:
    result = await session.scalars(
        select(OutgoingMessage)
        .where(OutgoingMessage.status == DeliveryStatus.PENDING)
        .order_by(OutgoingMessage.priority.desc(), OutgoingMessage.created_at.asc())
        .limit(limit)
    )
    return list(result)


async def list_timeout_candidates(session: AsyncSession) -> list[InterviewSession]:
    """Активные сессии, отсортированные по последней активности (для тайм-аутов)."""
    active = [
        SessionStatus.COLLECTING_DATA,
        SessionStatus.COLLECTING_RESUME,
        SessionStatus.INTERVIEWING,
    ]
    result = await session.scalars(
        select(InterviewSession)
        .where(InterviewSession.status.in_(active))
        .order_by(InterviewSession.last_activity_at.asc())
    )
    return list(result)


async def mark_message_sent(session: AsyncSession, message: OutgoingMessage) -> None:
    message.status = DeliveryStatus.SENT
    message.sent_at = utcnow()
    message.attempts += 1


async def mark_message_failed(
    session: AsyncSession, message: OutgoingMessage, error: str
) -> None:
    message.attempts += 1
    message.last_error = error[:2000]
    if message.attempts >= 5:
        message.status = DeliveryStatus.FAILED


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


async def get_setting(session: AsyncSession, key: str) -> dict | None:
    record = await session.get(Setting, key)
    return record.value if record else None


async def set_setting(session: AsyncSession, key: str, value: dict) -> None:
    record = await session.get(Setting, key)
    if record is None:
        session.add(Setting(key=key, value=value))
    else:
        record.value = value
        record.updated_at = utcnow()


__all__ = [
    "add_audit_event",
    "create_candidate",
    "create_or_update_candidate",
    "create_prompt_version",
    "create_scenario_version",
    "create_session",
    "create_vacancy",
    "enqueue_message",
    "get_active_session",
    "get_candidate",
    "get_candidate_by_telegram",
    "get_prompt_version",
    "get_scenario_version",
    "get_session_record",
    "get_setting",
    "get_vacancy",
    "get_vacancy_by_slug",
    "latest_report_for_session",
    "latest_resume_for_session",
    "list_active_vacancies",
    "list_answers",
    "list_timeout_candidates",
    "list_vacancies",
    "mark_message_failed",
    "mark_message_sent",
    "pending_messages",
    "save_answer",
    "save_report",
    "save_resume",
    "set_setting",
    "gen_uuid",
]