"""Веб-панель администрирования (FastAPI + Jinja2, базовая авторизация).

Раздел 5 TZ: дашборд со статистикой, вакансии, сессии, карточки кандидатов,
аудит, очередь доставки и настройки. Авторизация — HTTP Basic.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.base import get_session_factory
from app.database.models import (
    AIReport,
    AuditLog as AuditEvent,
    Candidate,
    InterviewSession,
    OutgoingMessage,
    Vacancy,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
security = HTTPBasic()

from app.database import repositories as repo  # noqa: E402


async def repo_audit(session: AsyncSession, event_type: str, **data) -> None:
    await repo.add_audit_event(session, event_type, **data)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="AI Interview Admin", docs_url=None, redoc_url=None)
    app.state.settings = settings

    factory = get_session_factory()

    async def db() -> Iterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        ok_user = secrets.compare_digest(credentials.username, settings.admin_username)
        ok_pass = secrets.compare_digest(credentials.password, settings.admin_password)
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Basic"},
                detail="Неверные учётные данные",
            )

    # ------------------------------------------------------------------
    # Страницы
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        total_candidates = (
            await session.scalar(select(func.count(Candidate.id))) or 0
        )
        total_sessions = (
            await session.scalar(select(func.count(InterviewSession.id))) or 0
        )
        status_rows = (
            await session.execute(
                select(InterviewSession.status, func.count(InterviewSession.id)).group_by(
                    InterviewSession.status
                )
            )
        ).all()
        rec_rows = (
            await session.execute(
                select(Candidate.status, func.count(Candidate.id)).group_by(Candidate.status)
            )
        ).all()
        pending_outbox = (
            await session.scalar(
                select(func.count(OutgoingMessage.id)).where(
                    OutgoingMessage.status == "pending"
                )
            )
            or 0
        )
        vacancies = (
            await session.scalars(
                select(Vacancy).order_by(Vacancy.created_at.desc()).limit(20)
            )
        ).all()

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "total_candidates": total_candidates,
                "total_sessions": total_sessions,
                "status_rows": status_rows,
                "rec_rows": rec_rows,
                "pending_outbox": pending_outbox,
                "vacancies": vacancies,
                "settings": settings,
            },
        )

    @app.get("/vacancies", response_class=HTMLResponse)
    async def vacancies_page(
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        rows = (
            await session.scalars(select(Vacancy).order_by(Vacancy.created_at.desc()))
        ).all()
        return templates.TemplateResponse(
            request, "vacancies.html", {"vacancies": rows}
        )

    @app.get("/sessions", response_class=HTMLResponse)
    async def sessions_page(
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
        candidate_id: str | None = None,
    ):
        stmt = (
            select(InterviewSession)
            .join(
                Candidate, Candidate.id == InterviewSession.candidate_id, isouter=True
            )
            .order_by(InterviewSession.started_at.desc())
            .limit(500)
        )
        if candidate_id:
            stmt = stmt.where(InterviewSession.candidate_id == candidate_id)
        rows = (await session.execute(stmt)).all()
        # Оценка и маршрут из последнего отчёта AI по каждой сессии
        report_by_session: dict[str, AIReport] = {}
        if rows:
            sids = [row[0].id for row in rows]
            reports = await session.scalars(
                select(AIReport)
                .where(AIReport.session_id.in_(sids))
                .order_by(AIReport.created_at.asc())
            )
            for rep in reports:
                report_by_session[rep.session_id] = rep
        return templates.TemplateResponse(
            request,
            "sessions.html",
            {
                "rows": rows,
                "candidate_id": candidate_id,
                "report_by_session": report_by_session,
            },
        )

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    async def candidate_detail(
        candidate_id: str,
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        candidate = await session.get(Candidate, candidate_id)
        if candidate is None:
            raise HTTPException(404, "Кандидат не найден")
        sessions = (
            await session.scalars(
                select(InterviewSession)
                .where(InterviewSession.candidate_id == candidate_id)
                .order_by(InterviewSession.started_at.desc())
            )
        ).all()
        audits = []
        reports = []
        if sessions:
            sids = [s.id for s in sessions]
            audits = (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.session_id.in_(sids))
                    .order_by(AuditEvent.created_at.desc())
                    .limit(200)
                )
            ).all()
            reports = (
                await session.scalars(
                    select(AIReport)
                    .where(AIReport.session_id.in_(sids))
                    .order_by(AIReport.created_at.asc())
                )
            ).all()
        report_by_session = {rep.session_id: rep for rep in reports}
        return templates.TemplateResponse(
            request,
            "candidate.html",
            {
                "candidate": candidate,
                "sessions": sessions,
                "audits": audits,
                "report_by_session": report_by_session,
            },
        )

    @app.get("/messages", response_class=HTMLResponse)
    async def messages_page(
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        rows = (
            await session.scalars(
                select(OutgoingMessage)
                .order_by(OutgoingMessage.created_at.desc())
                .limit(200)
            )
        ).all()
        return templates.TemplateResponse(
            request, "messages.html", {"messages": rows}
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(
        request: Request,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        rows = (
            await session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(500)
            )
        ).all()
        return templates.TemplateResponse(request, "audit.html", {"events": rows})

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, _: None = Depends(auth)):
        return templates.TemplateResponse(
            request, "settings.html", {"settings": settings}
        )

    @app.post("/candidates/{candidate_id}/delete")
    async def delete_candidate(
        candidate_id: str,
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        """Удаление ПД кандидата по установленной процедуре (раздел 7 TZ)."""
        from app.services.retention_service import delete_candidate_data

        removed = await delete_candidate_data(
            session, candidate_id, settings, reason="admin_request"
        )
        if not removed:
            raise HTTPException(404, "Кандидат не найден")
        return RedirectResponse("/sessions", status_code=303)

    @app.post("/retention/purge")
    async def retention_purge(
        _: None = Depends(auth),
        session: AsyncSession = Depends(db),
    ):
        """Ручной запуск очистки ПД по сроку хранения."""
        from app.services.retention_service import purge_expired

        removed = await purge_expired(settings)
        await repo_audit(session, "retention_purged", removed=removed)
        return RedirectResponse("/settings", status_code=303)

    # ------------------------------------------------------------------
    # API (для обновлений страницы)
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health(session: AsyncSession = Depends(db)):
        await session.scalar(select(Vacancy.id).limit(1))
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthz():
        """Healthcheck без авторизации (для Docker HEALTHCHECK)."""
        return {"status": "ok"}

    return app