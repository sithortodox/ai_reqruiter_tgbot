"""Маршрутизация A/B/manual_review и запрет авто-отказа при ошибке (4.7 TZ)."""

from __future__ import annotations

import pytest  # noqa: F401

from app.database.models import AnalysisStatus, CandidateStatus
from app.integrations.llm.schema import AIReportSchema
from app.services.ai_analysis_service import AnalysisOutcome
from app.services.routing_service import _requires_manual_review, decide


def make_report(**overrides) -> AIReportSchema:
    data = {
        "score": 9,
        "recommendation": "next_stage",
        "mandatory_requirements_met": True,
        "insufficient_data": False,
        "confidence": "high",
        "stop_factors": [],
        "mandatory_unmet": [],
    }
    data.update(overrides)
    return AIReportSchema.model_validate(data)


def make_vacancy(settings, threshold: int = 7):
    from app.database.models import Vacancy

    return Vacancy(
        slug="s",
        title="Тест",
        evaluation_config={"score_threshold": threshold},
    )


def test_branch_a_recommended(settings):
    outcome = AnalysisOutcome(status=AnalysisStatus.OK, report=make_report())
    result = decide(outcome, make_vacancy(settings), settings)
    assert result.status == CandidateStatus.RECOMMENDED
    assert result.high_priority is True
    assert result.notify_recruiters is True


def test_branch_b_not_recommended(settings):
    outcome = AnalysisOutcome(
        status=AnalysisStatus.OK,
        report=make_report(score=3, recommendation="reject",
                           mandatory_requirements_met=False,
                           mandatory_unmet=["Нет опыта"]),
    )
    result = decide(outcome, make_vacancy(settings, threshold=7), settings)
    assert result.status == CandidateStatus.NOT_RECOMMENDED
    assert result.notify_recruiters is False


def test_failure_routes_to_manual_review(settings):
    """Ошибка анализа НИКОГДА не даёт автоматический отказ."""
    for status in (AnalysisStatus.FAILED, AnalysisStatus.INVALID_SCHEMA):
        outcome = AnalysisOutcome(status=status, report=None, error_code="x")
        result = decide(outcome, make_vacancy(settings), settings)
        assert result.status == CandidateStatus.MANUAL_REVIEW
        assert result.notify_recruiters is True


def test_near_threshold_goes_manual(settings):
    """Оценка у порога (±1) — ручная проверка."""
    outcome = AnalysisOutcome(status=AnalysisStatus.OK, report=make_report(score=7))
    assert _requires_manual_review(make_report(score=7), 7)
    assert _requires_manual_review(make_report(score=6), 7)
    assert _requires_manual_review(make_report(score=8), 7)
    result = decide(outcome, make_vacancy(settings, threshold=7), settings)
    assert result.status == CandidateStatus.MANUAL_REVIEW


def test_low_confidence_goes_manual(settings):
    outcome = AnalysisOutcome(
        status=AnalysisStatus.OK, report=make_report(score=5, confidence="low")
    )
    result = decide(outcome, make_vacancy(settings), settings)
    assert result.status == CandidateStatus.MANUAL_REVIEW


def test_insufficient_data_goes_manual(settings):
    outcome = AnalysisOutcome(
        status=AnalysisStatus.OK, report=make_report(insufficient_data=True)
    )
    result = decide(outcome, make_vacancy(settings), settings)
    assert result.status == CandidateStatus.MANUAL_REVIEW


def test_stop_factor_contradiction_goes_manual(settings):
    outcome = AnalysisOutcome(
        status=AnalysisStatus.OK,
        report=make_report(score=9, stop_factors=["Стоп"]),
    )
    result = decide(outcome, make_vacancy(settings), settings)
    assert result.status == CandidateStatus.MANUAL_REVIEW