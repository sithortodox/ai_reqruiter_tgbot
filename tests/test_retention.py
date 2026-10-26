"""Тесты ретеншна и удаления персональных данных (раздел 7 TZ)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import repositories as repo
from app.database.models import (
    AuditLog,
    InterviewSession,
    SessionStatus,
)
from app.services import retention_service


async def make_finished_session(
    db, vacancy, *, telegram_id: int, completed_days_ago: int, tmp_path
):
    candidate = await repo.create_or_update_candidate(
        db, telegram_id=telegram_id, telegram_username=f"user{telegram_id}"
    )
    candidate.full_name = "Иван Петров"
    candidate.contact_data = "+79991234567"

    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=1,
        prompt_version=1,
        scenario_snapshot=[],
    )
    record.status = SessionStatus.COMPLETED
    record.completed_at = datetime.now(timezone.utc) - timedelta(days=completed_days_ago)
    record.candidate_status = "recommended"

    await repo.save_answer(
        db,
        session_id=record.id,
        question_id="q1",
        question_text="Вопрос",
        answer_text="Ответ с личными данными",
    )
    await repo.save_report(
        db,
        session_id=record.id,
        score=9,
        recommendation="next_stage",
        report_json={"summary": "Сводка", "strengths": ["a"]},
        provider="demo",
        model_name="demo-stub",
        prompt_version=1,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        duration_ms=10,
        attempts=1,
        error_code=None,
        analysis_status="ok",
    )
    # Файл резюме
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    fpath = storage / f"{candidate.id}.pdf"
    fpath.write_bytes(b"%PDF-1.4 test")
    await repo.save_resume(
        db,
        candidate_id=candidate.id,
        session_id=record.id,
        file_name="cv.pdf",
        mime_type="application/pdf",
        storage_reference=str(fpath),
        extracted_text="текст резюме с телефоном +7999",
    )
    return candidate, record, fpath


async def test_delete_candidate_data_removes_pd(db, vacancy_factory, settings, tmp_path):
    vacancy = await vacancy_factory()
    candidate, record, fpath = await make_finished_session(
        db, vacancy, telegram_id=101, completed_days_ago=1, tmp_path=tmp_path
    )

    ok = await retention_service.delete_candidate_data(
        db, candidate.id, settings, reason="test"
    )
    assert ok is True

    # Кандидат удалён
    assert await repo.get_candidate(db, candidate.id) is None
    # Сессия осталась, но обезличена
    remaining = await db.get(InterviewSession, record.id)
    assert remaining is not None
    assert remaining.candidate_id is None
    assert remaining.candidate_status == "recommended"
    # Ответы (ПД) удалены
    answers = await repo.list_answers(db, record.id)
    assert answers == []
    # Файл удалён с диска
    assert not fpath.exists()
    # Отчёт обезличен, но метрики сохранены
    report = await repo.latest_report_for_session(db, record.id)
    assert report is not None
    assert report.report_json == {}
    assert report.score == 9
    # Событие аудита записано
    events = (
        await db.scalars(
            select(AuditLog).where(AuditLog.event_type == "personal_data_deleted")
        )
    ).all()
    assert len(events) == 1
    assert events[0].event_data["reason"] == "test"


async def test_delete_unknown_candidate(db, settings):
    ok = await retention_service.delete_candidate_data(db, "nope", settings)
    assert ok is False


async def test_purge_expired_removes_old_keeps_recent(db, vacancy_factory, settings, tmp_path):
    vacancy = await vacancy_factory()
    old_cand, _, _ = await make_finished_session(
        db, vacancy, telegram_id=201,
        completed_days_ago=settings.retention_days + 10, tmp_path=tmp_path,
    )
    fresh_cand, _, _ = await make_finished_session(
        db, vacancy, telegram_id=202, completed_days_ago=1, tmp_path=tmp_path,
    )

    removed = await retention_service.purge_expired_in(db, settings)

    assert removed >= 1
    assert await repo.get_candidate(db, old_cand.id) is None
    assert await repo.get_candidate(db, fresh_cand.id) is not None


async def test_purge_skips_unfinished_sessions(db, vacancy_factory, settings):
    """Незавершённые сессии не попадают в ретеншн."""
    vacancy = await vacancy_factory()
    candidate = await repo.create_or_update_candidate(db, telegram_id=301, telegram_username=None)
    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=1,
        prompt_version=1,
        scenario_snapshot=[],
    )
    # Статус — не терминальный, completed_at не проставлен
    record.status = SessionStatus.INTERVIEWING

    await retention_service.purge_expired_in(db, settings)
    assert await repo.get_candidate(db, candidate.id) is not None