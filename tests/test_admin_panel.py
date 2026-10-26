"""Веб-панель администрирования (раздел 13, п.13: доступ без прав запрещён)."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database.base import get_engine, reset_database_state
from app.database import models  # noqa: F401 - регистрация моделей


@pytest.fixture
def panel(db, settings, tmp_path):
    """Приложение панели поверх тестовой БД + TestClient с валидным Basic."""
    reset_database_state()
    get_engine(settings.database_url)
    from app.admin.web import create_app

    app = create_app(settings)
    token = base64.b64encode(
        f"{settings.admin_username}:{settings.admin_password}".encode()
    ).decode()
    client = TestClient(app)
    client.headers["Authorization"] = f"Basic {token}"
    yield client
    reset_database_state()


def _bad_auth(settings: Settings) -> dict[str, str]:
    token = base64.b64encode(f"{settings.admin_username}:wrong-password".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def test_dashboard_requires_auth(panel, settings):
    """Запрос без авторизации и с неверным паролем отклоняется."""
    anon = TestClient(panel.app)
    assert anon.get("/").status_code == 401
    assert panel.get("/", headers=_bad_auth(settings)).status_code == 401


async def test_panel_pages_render(panel):
    """Все основные страницы панели отдаются без ошибок рендера."""
    for path in ("/", "/settings", "/sessions", "/vacancies", "/messages", "/audit"):
        response = panel.get(path)
        assert response.status_code == 200, path


async def test_candidate_delete_requires_auth(panel, settings, vacancy_factory, db):
    """Операции с ПД недоступны без административных прав."""
    from app.database import repositories as repo

    vacancy = await vacancy_factory()
    candidate = await repo.create_candidate(
        db, telegram_id=424242, telegram_username="u", full_name="Иван"
    )
    await db.commit()

    anon = TestClient(panel.app)
    response = anon.post(
        f"/candidates/{candidate.id}/delete", headers=_bad_auth(settings)
    )
    assert response.status_code == 401

    stored = await db.get(models.Candidate, candidate.id)
    assert stored is not None  # ПД не удалены при неавторизованном запросе
    assert vacancy is not None