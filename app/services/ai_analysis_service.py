"""Модуль AI-аналитики: контекст, вызов LLM, валидация, повторы (разделы 4.5–4.6)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import repositories as repo
from app.database.models import AnalysisStatus, InterviewSession, Vacancy
from app.integrations.llm import LLMAdapter, LLMError, get_adapter
from app.integrations.llm.schema import AIReportSchema, ReportValidationError, validate_report
from app.services import interview_service, resume_service

logger = logging.getLogger(__name__)


@dataclass
class AnalysisOutcome:
    """Результат анализа для модуля маршрутизации."""

    status: str  # ok | failed | invalid_schema
    report: AIReportSchema | None = None
    report_id: str | None = None
    error_code: str | None = None
    attempts: int = 0


def _price(settings: Settings, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Расчёт стоимости вызова, если заданы цены."""
    if settings.llm_price_input_per_1k is None or settings.llm_price_output_per_1k is None:
        return None
    inp = (input_tokens or 0) / 1000 * settings.llm_price_input_per_1k
    out = (output_tokens or 0) / 1000 * settings.llm_price_output_per_1k
    return round(inp + out, 6)


async def build_user_prompt(
    db: AsyncSession,
    record: InterviewSession,
    vacancy: Vacancy,
    settings: Settings,
) -> str:
    """Собирает контекст анализа: вакансия + ответы + резюме (ограниченно)."""
    answers = await repo.list_answers(db, record.id)
    resume = await repo.latest_resume_for_session(db, record.id)

    eval_cfg = vacancy.evaluation_config or {}
    threshold = int(eval_cfg.get("score_threshold", settings.default_score_threshold))

    resume_text = ""
    if resume is not None and resume.extracted_text:
        resume_text = resume_service.sanitize_for_prompt(
            resume_service.truncate_for_llm(
                resume.extracted_text, settings.llm_max_input_chars
            ),
            settings.llm_max_input_chars,
        )

    answers_block = []
    for ans in answers:
        safe_answer = resume_service.sanitize_for_prompt(ans.answer_text, 4000)
        answers_block.append(
            {"question": ans.question_text, "answer": safe_answer}
        )

    context = {
        "vacancy": {
            "title": vacancy.title,
            "description": vacancy.description,
            "mandatory_requirements": vacancy.mandatory_requirements,
            "preferred_skills": vacancy.preferred_skills,
            "score_threshold": threshold,
            "stop_factors": eval_cfg.get("stop_factors", []),
            "weights": eval_cfg.get("weights", {}),
        },
        "candidate": {
            # Имя и контакты НЕ передаются — только профессиональные данные
            "telegram_id": "скрыт",
            "has_resume": bool(resume_text),
        },
        "resume_text": resume_text or "[резюме не предоставлено]",
        "answers": answers_block,
        "output_schema": _schema_hint(),
    }
    return (
        "Проанализируй данные кандидата и верни отчёт строго в JSON по схеме.\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )


def _schema_hint() -> dict:
    """Краткая схема полей для промпта (полная проверка — в validate_report)."""
    return {
        "candidate_id": "строка",
        "vacancy_id": "строка",
        "score": "целое 1..10",
        "summary": "строка",
        "skills": [{"name": "строка", "evidence": "строка", "level": "junior|middle|senior|unknown"}],
        "relevant_experience": ["строка"],
        "strengths": ["строка"],
        "gaps": ["строка"],
        "mandatory_unmet": ["строка"],
        "stop_factors": ["строка"],
        "mandatory_requirements_met": "boolean",
        "insufficient_data": "boolean",
        "recommendation": "next_stage|reject|manual_review",
        "reasoning_summary": "строка",
        "confidence": "low|medium|high",
        "facts_to_verify": ["строка"],
    }


async def run_analysis(
    db: AsyncSession,
    record: InterviewSession,
    vacancy: Vacancy,
    settings: Settings,
    adapter: LLMAdapter | None = None,
) -> AnalysisOutcome:
    """Полный цикл анализа с ограниченными повторами.

    Никогда не кидает исключение наружу: ошибки превращаются в outcome
    со статусом failed, чтобы не приводить к автоматическому отказу.
    """
    adapter = adapter or get_adapter(settings)
    prompt_version, system_prompt = await interview_service.get_prompt(db, vacancy)
    user_prompt = await build_user_prompt(db, record, vacancy, settings)

    started = time.monotonic()
    attempts = 0
    last_code = "unknown"

    for attempt in range(1, settings.llm_max_retries + 2):
        attempts = attempt
        try:
            result = await adapter.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
            )
            report = validate_report(result.content)
            duration_ms = int((time.monotonic() - started) * 1000)
            cost = _price(settings, result.input_tokens, result.output_tokens)

            report.candidate_id = record.candidate_id
            report.vacancy_id = vacancy.id

            report_row = await repo.save_report(
                db,
                session_id=record.id,
                score=report.score,
                recommendation=report.recommendation,
                report_json=report.model_dump(),
                provider=result.provider,
                model_name=result.model,
                prompt_version=prompt_version,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=cost,
                duration_ms=duration_ms,
                attempts=attempt,
                error_code=None,
                analysis_status=AnalysisStatus.OK,
            )
            await repo.add_audit_event(
                db,
                "analysis_success",
                session_id=record.id,
                score=report.score,
                recommendation=report.recommendation,
                attempts=attempt,
                duration_ms=duration_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=cost,
            )
            return AnalysisOutcome(
                status=AnalysisStatus.OK,
                report=report,
                report_id=report_row.id,
                attempts=attempt,
            )

        except ReportValidationError as exc:
            last_code = "invalid_schema"
            logger.warning(
                "Схема отчёта не прошла валидацию (попытка %s): %s %s",
                attempt, exc.message, exc.details,
            )
        except LLMError as exc:
            last_code = exc.kind
            logger.warning("LLM ошибка (попытка %s): %s", attempt, exc)
            if exc.kind in {"auth", "rate_limit"}:
                # Нет смысла повторять сразу
                break
        except Exception as exc:  # noqa: BLE001
            last_code = "unexpected"
            logger.exception("Непредвиденная ошибка анализа: %s", exc)
            break

    # Неудача: сохраняем контролируемое состояние анализа
    duration_ms = int((time.monotonic() - started) * 1000)
    status = (
        AnalysisStatus.INVALID_SCHEMA
        if last_code == "invalid_schema"
        else AnalysisStatus.FAILED
    )
    report_row = await repo.save_report(
        db,
        session_id=record.id,
        score=None,
        recommendation=None,
        report_json={},
        provider=adapter.provider,
        model_name=adapter.model,
        prompt_version=prompt_version,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        duration_ms=duration_ms,
        attempts=attempts,
        error_code=last_code,
        analysis_status=status,
    )
    await repo.add_audit_event(
        db,
        "analysis_failed",
        session_id=record.id,
        error_code=last_code,
        attempts=attempts,
        duration_ms=duration_ms,
    )
    return AnalysisOutcome(
        status=status,
        report=None,
        report_id=report_row.id,
        error_code=last_code,
        attempts=attempts,
    )


async def retry_analysis(
    db: AsyncSession,
    record: InterviewSession,
    vacancy: Vacancy,
    settings: Settings,
) -> AnalysisOutcome:
    """Повторный запуск анализа для ручного разбора (manual_review)."""
    from app.database.models import SessionStatus

    # ANALYSIS_FAILED -> ANALYZING разрешён в ALLOWED_TRANSITIONS
    if record.status == SessionStatus.ANALYSIS_FAILED:
        interview_service.transition(record, SessionStatus.ANALYZING)
    return await run_analysis(db, record, vacancy, settings)