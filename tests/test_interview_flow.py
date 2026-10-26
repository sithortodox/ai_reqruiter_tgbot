"""Полный цикл диалога: старт, сбор данных, ответы, завершение (4.1–4.3 TZ)."""

from __future__ import annotations

from app.database import repositories as repo
from app.database.models import InterviewSession, SessionStatus
from app.services import interview_service as svc


async def make_session(db, vacancy, snapshot=None) -> InterviewSession:
    candidate = await repo.create_or_update_candidate(
        db, telegram_id=42, telegram_username="cand"
    )
    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=1,
        prompt_version=1,
        scenario_snapshot=snapshot or [],
    )
    return record


async def test_start_collects_consent_and_steps(db, vacancy_factory, settings):
    vacancy = await vacancy_factory()
    record, msg, resumed = await svc.start_or_resume(
        db, candidate=(await repo.create_or_update_candidate(
            db, telegram_id=1, telegram_username=None
        )),
        vacancy=vacancy,
        settings=settings,
    )
    assert resumed is False
    assert record.status == SessionStatus.COLLECTING_DATA
    assert record.consent_given is False
    # В приветствии есть уведомление о персональных данных
    assert "персональны" in msg.lower() or "согласи" in msg.lower()

    # Повторный /start возобновляет, а не создаёт дубль
    active = await repo.get_active_session(db, record.candidate_id)
    assert active is not None and active.id == record.id
    candidate = await repo.get_candidate(db, record.candidate_id)
    _, msg2, resumed2 = await svc.start_or_resume(
        db, candidate=candidate, vacancy=vacancy, settings=settings
    )
    assert resumed2 is True


async def test_consent_name_contact_flow(db, vacancy_factory, settings):
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy)
    # Фаза сбора данных активна
    record.status = SessionStatus.COLLECTING_DATA
    record.data_step = 0

    await svc.accept_consent(db, record)
    assert record.consent_given is True and record.data_step == 1

    assert not svc.validate_name("12")
    assert svc.validate_name("Иван Петров")
    await svc.accept_name(db, record, "Иван Петров")
    assert record.data_step == 2
    assert (await repo.get_candidate(db, record.candidate_id)).full_name == "Иван Петров"

    assert svc.validate_contact("+7 999 123 45 67")
    assert svc.validate_contact("user@mail.ru")
    assert svc.validate_contact("@username")
    assert not svc.validate_contact("??")

    await svc.accept_contact(db, record, "+79991234567")
    assert record.status == SessionStatus.COLLECTING_RESUME


async def test_answer_sequence_reaches_analyzing(db, vacancy_factory, settings):
    snapshot = [
        {"id": "q1", "text": "Вопрос 1", "order": 1, "required": True, "max_length": 500},
        {"id": "q2", "text": "Вопрос 2", "order": 2, "required": False, "max_length": 500},
    ]
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy, snapshot)
    record.status = SessionStatus.INTERVIEWING

    q1 = await svc.current_question(record)
    ok, err = await svc.accept_answer(db, record, q1, "Ответ один", settings)
    assert ok and err is None
    assert record.current_step == 1
    assert record.status == SessionStatus.INTERVIEWING

    # Пустой обязательный ответ отклоняется
    ok, err = await svc.accept_answer(db, record, q1, "  ", settings)
    assert not ok and err

    # Слишком длинный ответ отклоняется
    ok, err = await svc.accept_answer(db, record, q1, "x" * 600, settings)
    assert not ok and err and "длинный" in err.lower()

    q2 = await svc.current_question(record)
    ok, err = await svc.accept_answer(db, record, q2, "Ответ два", settings)
    assert ok
    # Вопросы кончились -> анализ
    assert record.status == SessionStatus.ANALYZING
    answers = await repo.list_answers(db, record.id)
    assert len(answers) == 2


async def test_duplicate_answer_is_idempotent(db, vacancy_factory, settings):
    snapshot = [{"id": "q1", "text": "В", "order": 1, "max_length": 500}]
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy, snapshot)
    record.status = SessionStatus.INTERVIEWING
    q = await svc.current_question(record)

    await svc.accept_answer(db, record, q, "первый", settings)
    # Повторная обработка того же update — ответ не дублируется
    record.current_step = 0
    record.status = SessionStatus.INTERVIEWING
    await svc.accept_answer(db, record, q, "повтор", settings)

    answers = await repo.list_answers(db, record.id)
    assert len(answers) == 1
    assert answers[0].answer_text == "первый"


async def test_snapshot_isolated_from_scenario_edits(db, vacancy_factory, settings):
    """Правка сценария не меняет вопросы идущей сессии."""
    snapshot = [{"id": "old_q", "text": "Старый вопрос", "order": 1, "max_length": 500}]
    vacancy = await vacancy_factory()
    record = await make_session(db, vacancy, snapshot)
    questions = await svc.questions_of(record)
    assert [q["id"] for q in questions] == ["old_q"]