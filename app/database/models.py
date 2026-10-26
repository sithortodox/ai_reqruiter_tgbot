"""Модели данных: кандидаты, вакансии, сессии, ответы, резюме, отчёты, журнал.

Структура соответствует разделу 5 TZ с расширениями:
- версионирование сценария вопросов и системного промпта (scenario_versions, prompt_versions);
- ключи идемпотентности для защиты от дублей уведомлений (idempotency_key);
- outbox для повторной доставки карточек рекрутерам;
- настройки в хранилище (settings) для веб-панели.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gen_uuid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Статусы
# ---------------------------------------------------------------------------


class SessionStatus(StrEnum):
    NEW = "new"
    COLLECTING_DATA = "collecting_data"
    COLLECTING_RESUME = "collecting_resume"
    INTERVIEWING = "interviewing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ANALYSIS_FAILED = "analysis_failed"
    MANUAL_REVIEW = "manual_review"


# Допустимые переходы между состояниями (раздел 6 TZ) — валидируемая модель.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    SessionStatus.NEW: {SessionStatus.COLLECTING_DATA, SessionStatus.CANCELLED},
    SessionStatus.COLLECTING_DATA: {
        SessionStatus.COLLECTING_RESUME,
        SessionStatus.INTERVIEWING,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.COLLECTING_RESUME: {
        SessionStatus.INTERVIEWING,
        SessionStatus.COLLECTING_RESUME,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.INTERVIEWING: {
        SessionStatus.INTERVIEWING,
        SessionStatus.ANALYZING,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.ANALYZING: {
        SessionStatus.COMPLETED,
        SessionStatus.ANALYSIS_FAILED,
        SessionStatus.MANUAL_REVIEW,
        SessionStatus.EXPIRED,
    },
    # Терминальные состояния: новые сообщения не должны их менять.
    SessionStatus.COMPLETED: set(),
    SessionStatus.CANCELLED: set(),
    SessionStatus.EXPIRED: set(),
    SessionStatus.ANALYSIS_FAILED: {SessionStatus.ANALYZING, SessionStatus.MANUAL_REVIEW},
    SessionStatus.MANUAL_REVIEW: {SessionStatus.COMPLETED, SessionStatus.CANCELLED},
}

TERMINAL_STATUSES = {
    SessionStatus.COMPLETED,
    SessionStatus.CANCELLED,
    SessionStatus.EXPIRED,
}


class CandidateStatus(StrEnum):
    RECOMMENDED = "recommended"
    NOT_RECOMMENDED = "not_recommended"
    MANUAL_REVIEW = "manual_review"
    IN_PROGRESS = "in_progress"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    INVALID_SCHEMA = "invalid_schema"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Сущности
# ---------------------------------------------------------------------------


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_data: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.IN_PROGRESS)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["InterviewSession"]] = relationship(
        back_populates="candidate",
        # ПД кандидата удаляются отдельно через retention_service (ON DELETE SET NULL)
        passive_deletes=True,
    )
    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="candidate", passive_deletes=True
    )


class Vacancy(Base):
    __tablename__ = "vacancies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    mandatory_requirements: Mapped[list] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[list] = mapped_column(JSON, default=list)
    # Порог, веса критериев, стоп-факторы, тексты сообщений, тайм-аут и пр.
    evaluation_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # Текущая версия сценария вопросов и промпта
    current_scenario_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("scenario_versions.id", use_alter=True), nullable=True
    )
    current_prompt_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("prompt_versions.id", use_alter=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["InterviewSession"]] = relationship(back_populates="vacancy")


class ScenarioVersion(Base):
    """Версия сценария (списка вопросов) вакансии."""

    __tablename__ = "scenario_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    vacancy_id: Mapped[str] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    consent_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("vacancy_id", "version", name="uq_scenario_vacancy_version"),
    )


class PromptVersion(Base):
    """Версия системного промпта AI-аналитики."""

    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    vacancy_id: Mapped[str] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("vacancy_id", "version", name="uq_prompt_vacancy_version"),
    )


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    # NULL после удаления ПД кандидата: сессия остаётся анонимной статистикой
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        ForeignKey("vacancies.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=SessionStatus.NEW, index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    # Фаза сбора данных: 0 - согласие, 1 - имя, 2 - контакт
    data_step: Mapped[int] = mapped_column(Integer, default=0)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    # Какая версия сценария/промпта зафиксирована для этой сессии
    scenario_version: Mapped[int] = mapped_column(Integer, default=1)
    prompt_version: Mapped[int] = mapped_column(Integer, default=1)
    # Снимок вопросов на момент старта: правки конфига не ломают идущие интервью
    scenario_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Защита от дублей при повторной обработке событий Telegram
    processed_update_ids: Mapped[list] = mapped_column(JSON, default=list)
    candidate_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    candidate: Mapped[Candidate | None] = relationship(
        back_populates="sessions", passive_deletes=True
    )
    vacancy: Mapped[Vacancy] = relationship(back_populates="sessions")
    answers: Mapped[list["InterviewAnswer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    reports: Mapped[list["AIReport"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class InterviewAnswer(Base):
    __tablename__ = "interview_answers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[str] = mapped_column(String(64))
    question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[InterviewSession] = relationship(back_populates="answers")

    __table_args__ = (
        # Один ответ на вопрос в рамках сессии (идемпотентность)
        UniqueConstraint("session_id", "question_id", name="uq_answer_session_question"),
    )


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    # NULL после удаления ПД кандидата
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    storage_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    candidate: Mapped[Candidate | None] = relationship(
        back_populates="resumes", passive_deletes=True
    )


class AIReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_status: Mapped[str] = mapped_column(
        String(32), default=AnalysisStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[InterviewSession] = relationship(back_populates="reports")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    event_data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (Index("ix_audit_event_created", "event_type", "created_at"),)


class Setting(Base):
    """Хранилище настроек админ-панели (контролируемое хранилище)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OutgoingMessage(Base):
    """Outbox: гарантированная доставка уведомлений с защитой от дублей."""

    __tablename__ = "outgoing_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=gen_uuid)
    # Уникальный ключ: одна карточка/сообщение отправляется ровно один раз
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    parse_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # высокий = 1
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)