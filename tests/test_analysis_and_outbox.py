"""Outbox: идемпотентность уведомлений и полный цикл анализа в демо-режиме."""

from __future__ import annotations

from app.database import repositories as repo
from app.database.models import AnalysisStatus, InterviewSession, SessionStatus
from app.services import ai_analysis_service, notification_service


async def make_session(db, vacancy) -> InterviewSession:
    candidate = await repo.create_or_update_candidate(
        db, telegram_id=77, telegram_username="cand"
    )
    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=1,
        prompt_version=1,
        scenario_snapshot=[
            {"id": "q1", "text": "Опыт?", "order": 1, "max_length": 500}
        ],
    )
    record.status = SessionStatus.INTERVIEWING
    await repo.save_answer(
        db,
        session_id=record.id,
        question_id="q1",
        question_text="Опыт?",
        answer_text="5 лет коммерческой разработки на Python, PostgreSQL, async, FastAPI, Docker, pytest",
    )
    record.status = SessionStatus.ANALYZING
    return record


async def test_enqueue_message_deduplicates(db):
    first = await repo.enqueue_message(
        db, idempotency_key="card:s1:0", chat_id=-1, text="карточка"
    )
    second = await repo.enqueue_message(
        db, idempotency_key="card:s1:0", chat_id=-1, text="карточка"
    )
    assert first is not None
    assert second is None  # дубль отброшен


async def test_recruiter_card_exactly_once(db, vacancy_factory, settings):
    """Одна карточка кандидата — ровно одно сообщение в очереди."""
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy)
    answers = [(a.question_text, a.answer_text) for a in await repo.list_answers(db, record.id)]
    kwargs = dict(
        record=record,
        vacancy=vacancy,
        candidate_name="Иван",
        contact="+7999",
        report={"summary": "Тест", "strengths": [], "gaps": []},
        routing_status="recommended",
        answers=answers,
        score=9,
        settings=settings,
        priority=1,
    )
    created1 = await notification_service.enqueue_recruiter_card(db, **kwargs)
    created2 = await notification_service.enqueue_recruiter_card(db, **kwargs)
    assert created1 >= 1
    assert created2 == 0  # повторный вызов не создаёт дублей
    pending = await repo.pending_messages(db, limit=50)
    assert len(pending) == created1


async def test_card_split_respects_telegram_limit(db, vacancy_factory, settings):
    """Очень длинные ответы не дают сообщение длиннее 4096 символов."""
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy)
    answers = [(f"Вопрос {i}", "очень длинный ответ " * 100) for i in range(30)]
    parts = notification_service.build_candidate_card(
        record=record,
        vacancy=vacancy,
        candidate_name="Иван",
        contact="+7999",
        report={"summary": "Сводка", "strengths": ["a"] * 20},
        routing_status="recommended",
        answers=answers,
        score=9,
    )
    assert parts
    assert all(len(p) <= 4096 for p in parts)


async def test_full_analysis_demo_mode(db, vacancy_factory, settings):
    """Полный цикл анализа: контекст -> LLM (демо) -> валидация -> отчёт."""
    from app.database.models import Vacancy

    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy)
    outcome = await ai_analysis_service.run_analysis(db, record, vacancy, settings)
    assert outcome.status == AnalysisStatus.OK
    assert outcome.report is not None
    assert 1 <= outcome.report.score <= 10
    assert outcome.report.recommendation in (
        "next_stage", "reject", "manual_review",
    )
    # Отчёт и токены сохранены
    saved = await repo.latest_report_for_session(db, record.id)
    assert saved is not None
    assert saved.analysis_status == AnalysisStatus.OK
    assert saved.attempts >= 1


async def test_analysis_context_excludes_name(db, vacancy_factory, settings):
    """Персональные данные кандидата не попадают в промпт LLM."""
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy)
    candidate = await repo.get_candidate(db, record.candidate_id)
    candidate.full_name = "Секретное Имя"
    candidate.contact_data = "secret@example.com"
    await db.flush()

    prompt = await ai_analysis_service.build_user_prompt(db, record, vacancy, settings)
    assert "Секретное Имя" not in prompt
    assert "secret@example.com" not in prompt
    # Но ответы кандидата присутствуют
    assert "PostgreSQL" in prompt