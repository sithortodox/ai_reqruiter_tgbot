"""Автомат состояний интервью: допустимые/недопустимые переходы (раздел 6 TZ)."""

from __future__ import annotations

import pytest

from app.database.models import ALLOWED_TRANSITIONS, SessionStatus
from app.services.interview_service import TransitionError, can_transition, transition


def test_all_transition_targets_are_valid_states():
    """Каждый переход указывает на существующее состояние."""
    valid = set(SessionStatus)
    for source, targets in ALLOWED_TRANSITIONS.items():
        assert source in valid, f"источник {source} не существует"
        for target in targets:
            assert target in valid, f"цель {source}->{target} не существует"


def test_happy_path_allowed():
    """new -> data -> resume -> interview -> analyzing -> completed."""
    assert can_transition(SessionStatus.NEW, SessionStatus.COLLECTING_DATA)
    assert can_transition(SessionStatus.COLLECTING_DATA, SessionStatus.COLLECTING_RESUME)
    assert can_transition(SessionStatus.COLLECTING_RESUME, SessionStatus.INTERVIEWING)
    assert can_transition(SessionStatus.INTERVIEWING, SessionStatus.ANALYZING)
    assert can_transition(SessionStatus.ANALYZING, SessionStatus.COMPLETED)


def test_terminal_states_frozen():
    """Терминальные состояния не имеют переходов (кроме ручной проверки)."""
    assert ALLOWED_TRANSITIONS[SessionStatus.COMPLETED] == set()
    assert ALLOWED_TRANSITIONS[SessionStatus.CANCELLED] == set()
    assert ALLOWED_TRANSITIONS[SessionStatus.EXPIRED] == set()
    for target in set(SessionStatus):
        assert not can_transition(SessionStatus.COMPLETED, target)


def test_error_analysis_goes_to_manual_or_retry():
    """Ошибки анализа ведут в ручную проверку или повторный анализ — не в отказ."""
    assert can_transition(SessionStatus.ANALYZING, SessionStatus.ANALYSIS_FAILED)
    assert can_transition(SessionStatus.ANALYSIS_FAILED, SessionStatus.MANUAL_REVIEW)
    assert can_transition(SessionStatus.ANALYSIS_FAILED, SessionStatus.ANALYZING)
    assert not can_transition(SessionStatus.ANALYSIS_FAILED, SessionStatus.COMPLETED)


async def test_transition_raises_on_illegal_move(db, vacancy_factory):
    """Нелегальный переход кидает TransitionError, не меняя статус."""
    from app.database import repositories as repo

    candidate = await repo.create_or_update_candidate(
        db, telegram_id=1, telegram_username="u"
    )
    vacancy = await vacancy_factory()
    record = await repo.create_session(
        db,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        scenario_version=1,
        prompt_version=1,
        scenario_snapshot=[],
    )
    with pytest.raises(TransitionError):
        transition(record, SessionStatus.COMPLETED)  # new -> completed запрещён
    assert record.status == SessionStatus.NEW

    # Легальный переход проходит и фиксирует время активности
    transition(record, SessionStatus.COLLECTING_DATA)
    assert record.status == SessionStatus.COLLECTING_DATA
    assert record.last_activity_at is not None