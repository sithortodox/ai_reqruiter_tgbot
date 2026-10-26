"""Подсистема доступа к данным (SQLAlchemy 2.0, асинхронный доступ)."""

from app.database.base import Base, get_engine, get_session_factory, session_scope
from app.database.models import (
    AIReport,
    AuditLog,
    Candidate,
    InterviewAnswer,
    InterviewSession,
    PromptVersion,
    Resume,
    ScenarioVersion,
    Setting,
    Vacancy,
)

__all__ = [
    "AIReport",
    "AuditLog",
    "Base",
    "Candidate",
    "InterviewAnswer",
    "InterviewSession",
    "PromptVersion",
    "Resume",
    "ScenarioVersion",
    "Setting",
    "Vacancy",
    "get_engine",
    "get_session_factory",
    "session_scope",
]