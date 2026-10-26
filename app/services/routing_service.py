"""Модуль маршрутизации: ветки A/B/manual_review (раздел 4.7 TZ)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import repositories as repo
from app.database.models import (
    AnalysisStatus,
    CandidateStatus,
    InterviewSession,
    SessionStatus,
    Vacancy,
)
from app.integrations.llm.schema import AIReportSchema
from app.services import interview_service
from app.services.ai_analysis_service import AnalysisOutcome

logger = logging.getLogger(__name__)


@dataclass
class RoutingResult:
    status: str  # recommended | not_recommended | manual_review
    notify_recruiters: bool
    high_priority: bool
    message_key: str  # ключ текста для кандидата
    reason: str


def decide(
    outcome: AnalysisOutcome,
    vacancy: Vacancy,
    settings: Settings,
) -> RoutingResult:
    """Определение маршрута по конфигурируемым критериям вакансии."""
    eval_cfg = vacancy.evaluation_config or {}
    threshold = int(eval_cfg.get("score_threshold", settings.default_score_threshold))

    # Техническая ошибка или невалидный ответ модели -> ручная проверка.
    # Отказ по технической ошибке запрещён (раздел 4.5, 4.7 TZ).
    if outcome.status != AnalysisStatus.OK or outcome.report is None:
        return RoutingResult(
            status=CandidateStatus.MANUAL_REVIEW,
            notify_recruiters=True,
            high_priority=False,
            message_key="analysis_error",
            reason=f"analysis_{outcome.status}:{outcome.error_code or 'unknown'}",
        )

    report = outcome.report

    if _requires_manual_review(report, threshold):
        return RoutingResult(
            status=CandidateStatus.MANUAL_REVIEW,
            notify_recruiters=True,
            high_priority=False,
            message_key="finished_manual_review",
            reason=_manual_reason(report, threshold),
        )

    if report.mandatory_requirements_met and report.score >= threshold:
        return RoutingResult(
            status=CandidateStatus.RECOMMENDED,
            notify_recruiters=True,
            high_priority=True,
            message_key="finished_recommended",
            reason=f"score {report.score} >= {threshold}, обязательные требования выполнены",
        )

    # Ветка B: несоответствие
    unmet = ", ".join(report.mandatory_unmet[:3]) or "порог не достигнут"
    return RoutingResult(
        status=CandidateStatus.NOT_RECOMMENDED,
        notify_recruiters=False,
        high_priority=False,
        message_key="finished_not_recommended",
        reason=f"score {report.score} < {threshold} или не выполнены требования: {unmet}",
    )


def _requires_manual_review(report: AIReportSchema, threshold: int) -> bool:
    if report.recommendation == "manual_review":
        return True
    if report.insufficient_data:
        return True
    if report.confidence == "low":
        return True
    if report.stop_factors and report.mandatory_requirements_met:
        # Стоп-фактор есть, но обязательные требования «выполнены» — противоречие
        return True
    if abs(report.score - threshold) <= 1:
        # Результат у порога — не однозначен автоматическому решению
        return True
    return False


def _manual_reason(report: AIReportSchema, threshold: int) -> str:
    if report.recommendation == "manual_review":
        return "модель рекомендовала ручную проверку"
    if report.insufficient_data:
        return "недостаточно данных для решения"
    if report.confidence == "low":
        return "низкая уверенность анализа"
    if report.stop_factors:
        return f"стоп-факторы при выполненных требованиях: {report.stop_factors[0]}"
    return f"оценка {report.score} у порога {threshold}"


async def apply_routing(
    db: AsyncSession,
    record: InterviewSession,
    vacancy: Vacancy,
    outcome: AnalysisOutcome,
    settings: Settings,
) -> RoutingResult:
    """Переводит сессию в финальное состояние и фиксирует статус кандидата."""
    result = decide(outcome, vacancy, settings)

    if result.status == CandidateStatus.MANUAL_REVIEW:
        if record.status == SessionStatus.ANALYZING:
            interview_service.transition(record, SessionStatus.MANUAL_REVIEW)
    else:
        # ANALYZING -> COMPLETED
        interview_service.transition(record, SessionStatus.COMPLETED)
        record.completed_at = datetime.now(timezone.utc)

    record.candidate_status = result.status
    candidate = await repo.get_candidate(db, record.candidate_id)
    if candidate is not None:
        candidate.status = result.status
        candidate.updated_at = datetime.now(timezone.utc)

    await repo.add_audit_event(
        db,
        "status_changed",
        session_id=record.id,
        status=result.status,
        reason=result.reason,
    )
    return result