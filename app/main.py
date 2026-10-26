"""Точка входа: инициализация БД, бот (long polling) и веб-панель."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from app.config import ConfigError, Settings, get_settings, require_for_bot
from app.logging_conf import setup_logging

logger = logging.getLogger(__name__)


async def init_database(settings: Settings) -> None:
    """Создание таблиц и сид демо-данных при первом запуске."""
    from sqlalchemy import select

    from app.database.base import Base, get_engine, session_scope
    from app.database import repositories as repo
    from app.database.models import Vacancy
    from app.seed import DEMO_QUESTIONS, DEMO_VACANCY, load_default_prompt

    engine = get_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_scope() as db:
        existing = await db.scalar(select(Vacancy).limit(1))
        if existing is None:
            vacancy = await repo.create_vacancy(
                db,
                slug=DEMO_VACANCY["slug"],
                title=DEMO_VACANCY["title"],
                description=DEMO_VACANCY["description"],
                mandatory_requirements=DEMO_VACANCY["mandatory_requirements"],
                preferred_skills=DEMO_VACANCY["preferred_skills"],
                evaluation_config={
                    **DEMO_VACANCY["evaluation_config"],
                },
                active=True,
            )
            scenario = await repo.create_scenario_version(
                db,
                vacancy.id,
                questions=DEMO_QUESTIONS,
                consent_text=DEMO_VACANCY["evaluation_config"]["texts"]["consent"],
            )
            prompt = await repo.create_prompt_version(
                db, vacancy.id, load_default_prompt()
            )
            vacancy.current_scenario_version_id = scenario.id
            vacancy.current_prompt_version_id = prompt.id
            logger.info("Создана демо-вакансия %s (slug=%s)", vacancy.id, vacancy.slug)

        # Лимиты из окружения по умолчанию в настройках хранилища
        await repo.set_setting(
            db,
            "runtime_limits",
            {
                "max_resume_size_mb": settings.max_resume_size_mb,
                "max_message_length": settings.max_message_length,
                "timeout_minutes": settings.interview_timeout_minutes,
            },
        )


async def run_bot(settings: Settings) -> None:
    from app.bot.dispatcher import create_bot, create_dispatcher
    from app.services.dispatch_service import outbox_worker
    from app.services.retention_service import retention_worker
    from app.services.timeout_service import timeout_worker

    bot = create_bot(settings)
    dp = create_dispatcher(settings)

    # Фоновые задачи: доставка outbox-очереди, тайм-ауты и ретеншн ПД
    outbox_task = asyncio.create_task(
        outbox_worker(bot, settings), name="outbox-worker"
    )
    timeout_task = asyncio.create_task(
        timeout_worker(settings, bot), name="timeout-worker"
    )
    retention_task = asyncio.create_task(
        retention_worker(settings), name="retention-worker"
    )
    logger.info("Запуск бота в режиме long polling…")
    try:
        # drop_pending_updates=True: при первом старте не проигрываем старые апдейты
        await dp.start_polling(bot, handle_signals=True)
    finally:
        for task in (outbox_task, timeout_task, retention_task):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(outbox_task, timeout_task, retention_task)
        await bot.session.close()


async def run_admin_web(settings: Settings) -> None:
    import uvicorn

    from app.admin.web import create_app

    app = create_app(settings)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.admin_web_port, log_level="warning"
    )
    server = uvicorn.Server(config)
    logger.info(
        "Админ-панель: http://0.0.0.0:%s", settings.admin_web_port
    )
    await server.serve()


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    require_for_bot(settings)

    await init_database(settings)

    tasks = [asyncio.create_task(run_bot(settings), name="bot")]
    if settings.admin_web_enabled:
        tasks.append(asyncio.create_task(run_admin_web(settings), name="admin-web"))

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(amain())
    except ConfigError as exc:
        print(f"❌ Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")


if __name__ == "__main__":
    main()