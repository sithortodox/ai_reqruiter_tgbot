"""Хендлеры Telegram-бота: регистрация, сбор данных, интервью, финиш.

Состояние интервью хранится в БД (InterrviewSession), а не в FSM — сессия
восстанавливается после перезапуска приложения без потерь (раздел 6 TZ).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    Contact,
    Message,
    TelegramObject,
)

from app.bot import keyboards as kb
from app.config import Settings
from app.database import repositories as repo
from app.database.models import (
    InterviewSession,
    SessionStatus,
    Vacancy,
)
from app.services import (
    ai_analysis_service,
    interview_service,
    notification_service,
    resume_service,
    routing_service,
)
from app.services.dispatch_service import flush_outbox

logger = logging.getLogger(__name__)

router = Router(name="main")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _settings(data: dict[str, Any]) -> Settings:
    return data["settings"]


async def _resolve_vacancy(
    db, settings: Settings, payload: str | None
) -> tuple[Vacancy | None, str | None]:
    """Deep-link ?start=<slug|id> -> вакансия; иначе дефолт."""
    vacancies = await repo.list_active_vacancies(db)
    if not vacancies:
        return None, None
    if payload:
        vacancy = await repo.get_vacancy_by_slug(db, payload)
        if vacancy is None:
            vacancy = await repo.get_vacancy(db, payload)
        if vacancy is not None and vacancy.active:
            return vacancy, None
        logger.warning("Вакансия %r не найдена — используется вакансия по умолчанию", payload)
    if settings.default_vacancy_id:
        vacancy = await repo.get_vacancy(db, settings.default_vacancy_id)
        if vacancy is not None and vacancy.active:
            return vacancy, None
    return vacancies[0], None


# ---------------------------------------------------------------------------
# /start, /help, /cancel
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, data: dict[str, Any]) -> None:
    db = data["db"]
    settings = _settings(data)
    tg_user = message.from_user
    assert tg_user is not None

    candidate = await repo.create_or_update_candidate(
        db,
        telegram_id=tg_user.id,
        telegram_username=tg_user.username,
    )

    payload = (command.args or "").strip()
    vacancy, _ = await _resolve_vacancy(db, settings, payload or None)
    if vacancy is None:
        await message.answer(
            "⚠️ Ни одна вакансия не настроена. Обратитесь к администратору бота."
        )
        return

    record, text, is_resumed = await interview_service.start_or_resume(
        db, candidate=candidate, vacancy=vacancy, settings=settings
    )
    await db.commit()

    texts = interview_service.texts_for(vacancy)
    if is_resumed:
        await message.answer(text, reply_markup=kb.remove_keyboard())
        await _continue_flow(message, db, record, vacancy, settings, data)
        return

    # Новый старт: text уже содержит приветствие + уведомление о ПД
    await message.answer(text, reply_markup=kb.consent_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(kb.HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, data: dict[str, Any]) -> None:
    db = data["db"]
    settings = _settings(data)
    tg_user = message.from_user
    assert tg_user is not None
    candidate = await repo.get_candidate_by_telegram(db, tg_user.id)
    if candidate is None:
        await message.answer("Сейчас нет активного интервью.")
        return
    record = await repo.get_active_session(db, candidate.id)
    if record is None:
        await message.answer("Сейчас нет активного интервью.")
        return
    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    if vacancy is None:
        await message.answer("Ошибка: вакансия не найдена.")
        return
    text = await interview_service.cancel_session(db, record, vacancy)
    await db.commit()
    await message.answer(text, reply_markup=kb.remove_keyboard())


# ---------------------------------------------------------------------------
# Согласие на обработку ПД
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("consent:"))
async def on_consent(callback: CallbackQuery, data: dict[str, Any]) -> None:
    db = data["db"]
    settings = _settings(data)
    assert callback.from_user is not None
    candidate = await repo.get_candidate_by_telegram(db, callback.from_user.id)
    if candidate is None:
        await callback.answer("Наберите /start", show_alert=True)
        return
    record = await repo.get_active_session(db, candidate.id)
    if record is None or record.status != SessionStatus.COLLECTING_DATA:
        await callback.answer("Интервью не в фазе согласия.", show_alert=True)
        return

    if callback.data == "consent:no":
        await callback.message.edit_text(
            "Без согласия на обработку персональных данных собеседование "
            "начать невозможно. Наберите /start, если передумаете.",
            reply_markup=None,
        )
        await callback.answer()
        return

    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    if vacancy is None:
        await callback.answer("Ошибка вакансии", show_alert=True)
        return

    await interview_service.accept_consent(db, record)
    await db.commit()
    texts = interview_service.texts_for(vacancy)
    await callback.message.edit_text("✅ Спасибо, согласие получено.")
    await callback.message.answer(
        texts["ask_name"], reply_markup=kb.remove_keyboard()
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Фаза сбора данных: имя -> контакт -> резюме
# ---------------------------------------------------------------------------


@router.message(F.contact)
async def on_contact(message: Message, data: dict[str, Any]) -> None:
    """Telegram-контакт кнопкой (4.2)."""
    db = data["db"]
    settings = _settings(data)
    assert message.from_user is not None
    candidate = await repo.get_candidate_by_telegram(db, message.from_user.id)
    if candidate is None:
        await message.answer("Наберите /start, чтобы начать.")
        return
    record = await repo.get_active_session(db, candidate.id)
    if record is None or record.status != SessionStatus.COLLECTING_DATA or record.data_step != 2:
        await message.answer("Сейчас контакт не запрашивается.")
        return
    contact: Contact = message.contact
    contact_text = f"{contact.phone_number}"
    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    assert vacancy is not None
    await interview_service.accept_contact(db, record, contact_text)
    await db.commit()
    await _ask_resume(message, record, vacancy, settings)


@router.message(F.text)
async def on_text(message: Message, data: dict[str, Any]) -> None:
    """Универсальный текстовый ввод по фазам сессии."""
    db = data["db"]
    settings = _settings(data)
    assert message.from_user is not None and message.text is not None

    candidate = await repo.get_candidate_by_telegram(db, message.from_user.id)
    if candidate is None:
        await message.answer("Наберите /start, чтобы начать собеседование.")
        return

    record = await repo.get_active_session(db, candidate.id)
    if record is None:
        await message.answer(
            "Нет активного интервью. Наберите /start, чтобы начать.",
        )
        return

    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    if vacancy is None:
        await message.answer("Ошибка: вакансия не найдена. Обратитесь к администратору.")
        return

    text = message.text.strip()
    status = record.status

    # --- Терминальные состояния: поздние сообщения не меняют результат ---
    if status in (
        SessionStatus.COMPLETED,
        SessionStatus.CANCELLED,
        SessionStatus.EXPIRED,
    ):
        await message.answer(
            "Это интервью уже завершено. Наберите /start, чтобы пройти заново."
        )
        return

    if status == SessionStatus.ANALYZING:
        await message.answer(interview_service.texts_for(vacancy)["analysis_running"])
        return

    if status == SessionStatus.ANALYSIS_FAILED:
        await message.answer(interview_service.texts_for(vacancy)["analysis_error"])
        return

    if status == SessionStatus.MANUAL_REVIEW:
        await message.answer(interview_service.texts_for(vacancy)["finished_manual_review"])
        return

    # --- Фаза сбора данных ---
    if status == SessionStatus.COLLECTING_DATA:
        texts = interview_service.texts_for(vacancy)
        if record.data_step == 0:
            if text.lower() in ("да", "yes", "согласен", "согласна", "✅"):
                await interview_service.accept_consent(db, record)
                await db.commit()
                await message.answer(texts["ask_name"])
            else:
                await message.answer(
                    "Пожалуйста, подтвердите согласие кнопкой ниже "
                    "или ответьте «да».",
                    reply_markup=kb.consent_keyboard(),
                )
            return
        if record.data_step == 1:
            if not interview_service.validate_name(text):
                await message.answer(
                    "Пожалуйста, укажите корректное имя (минимум 2 символа)."
                )
                return
            await interview_service.accept_name(db, record, text)
            await db.commit()
            await message.answer(
                texts["ask_contact"], reply_markup=kb.contact_keyboard()
            )
            return
        if record.data_step == 2:
            if not interview_service.validate_contact(text):
                await message.answer(
                    "Контакт не распознан. Укажите номер телефона, e-mail "
                    "или @username, либо отправьте Telegram-контакт кнопкой ниже.",
                    reply_markup=kb.contact_keyboard(),
                )
                return
            await interview_service.accept_contact(db, record, text)
            await db.commit()
            await _ask_resume(message, record, vacancy, settings)
            return

    # --- Фаза резюме ---
    if status == SessionStatus.COLLECTING_RESUME:
        # Текстовое резюме
        await _accept_text_resume(message, db, record, candidate.id, text, settings, vacancy)
        await db.commit()
        await _start_questions(message, record, vacancy, settings)
        return

    # --- Фаза интервью ---
    if status == SessionStatus.INTERVIEWING:
        question = await interview_service.current_question(record)
        if question is None:
            # Шаг устарел (например, после перезапуска) — завершаем сбор
            interview_service.transition(record, SessionStatus.ANALYZING)
            await db.commit()
            await _run_pipeline(message, db, record, vacancy, settings, data)
            return
        ok, err = await interview_service.accept_answer(
            db, record, question, text, settings
        )
        if not ok:
            await message.answer(err or "Некорректный ответ.")
            await db.commit()
            return
        await db.commit()

        if record.status == SessionStatus.ANALYZING:
            await message.answer(
                interview_service.texts_for(vacancy)["analysis_running"],
                reply_markup=kb.remove_keyboard(),
            )
            await _run_pipeline(message, db, record, vacancy, settings, data)
            return

        nxt = interview_service.next_question_text(record, vacancy)
        if nxt:
            q = await interview_service.current_question(record)
            markup = kb.skip_keyboard() if q and not q.get("required") else None
            await message.answer(nxt, reply_markup=markup)
        return

    await message.answer("Не удалось определить состояние. Наберите /start.")


@router.callback_query(F.data == "skip")
async def on_skip(callback: CallbackQuery, data: dict[str, Any]) -> None:
    """Пропуск необязательного вопроса."""
    db = data["db"]
    settings = _settings(data)
    assert callback.from_user is not None
    candidate = await repo.get_candidate_by_telegram(db, callback.from_user.id)
    if candidate is None:
        await callback.answer("Наберите /start", show_alert=True)
        return
    record = await repo.get_active_session(db, candidate.id)
    if record is None or record.status != SessionStatus.INTERVIEWING:
        await callback.answer("Нечего пропускать.", show_alert=True)
        return
    question = await interview_service.current_question(record)
    if question is None or question.get("required"):
        await callback.answer("Этот вопрос обязательный.", show_alert=True)
        return
    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    assert vacancy is not None

    ok, _ = await interview_service.accept_answer(
        db, record, question, "— (пропущено)", settings
    )
    await db.commit()

    if record.status == SessionStatus.ANALYZING:
        await callback.message.edit_text(
            interview_service.texts_for(vacancy)["analysis_running"]
        )
        await _run_pipeline(callback.message, db, record, vacancy, settings, data)
        await callback.answer()
        return

    nxt = interview_service.next_question_text(record, vacancy)
    if nxt:
        await callback.message.edit_text(nxt, reply_markup=None)
        q = await interview_service.current_question(record)
        if q and not q.get("required"):
            await callback.message.answer(
                "Можно пропустить:", reply_markup=kb.skip_keyboard()
            )
    await callback.answer()


# ---------------------------------------------------------------------------
# Документы (резюме)
# ---------------------------------------------------------------------------


@router.message(F.document)
async def on_document(message: Message, data: dict[str, Any]) -> None:
    db = data["db"]
    settings = _settings(data)
    assert message.from_user is not None
    candidate = await repo.get_candidate_by_telegram(db, message.from_user.id)
    if candidate is None:
        await message.answer("Наберите /start, чтобы начать.")
        return
    record = await repo.get_active_session(db, candidate.id)
    if record is None or record.status != SessionStatus.COLLECTING_RESUME:
        await message.answer("Сейчас резюме не запрашивается.")
        return
    vacancy = await repo.get_vacancy(db, record.vacancy_id)
    assert vacancy is not None
    texts = interview_service.texts_for(vacancy)
    doc = message.document
    assert doc is not None

    file_name = doc.file_name or "resume"
    mime = doc.mime_type or ""
    try:
        resume_service.validate_file(file_name, mime, doc.file_size or 0, settings)
    except resume_service.ResumeError as exc:
        await message.answer(_resume_error_text(exc, texts, settings))
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer(texts["file_too_big"].format(max_size=settings.max_resume_size_mb))
        return

    try:
        file = await message.bot.get_file(doc.file_id)
        buffer = await message.bot.download_file(file.file_path)
        raw = buffer.read() if buffer else b""
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось скачать файл %s", file_name)
        await message.answer(texts["file_unreadable"])
        return

    try:
        extracted = resume_service.extract_text(file_name, raw)
    except resume_service.ResumeError as exc:
        await message.answer(_resume_error_text(exc, texts, settings))
        return

    storage_ref = resume_service.save_to_storage(raw, file_name, settings)
    await repo.save_resume(
        db,
        candidate_id=candidate.id,
        session_id=record.id,
        file_name=file_name,
        mime_type=mime,
        storage_reference=storage_ref,
        extracted_text=extracted,
    )
    await repo.add_audit_event(
        db, "resume_received", session_id=record.id,
        file_name=file_name, chars=len(extracted),
    )
    await db.commit()
    await message.answer(f"✅ Резюме принято ({len(extracted)} символов).")
    await _start_questions(message, record, vacancy, settings)


def _resume_error_text(
    exc: resume_service.ResumeError, texts: dict, settings: Settings
) -> str:
    if exc.kind == "unsupported":
        return texts["unsupported_file"]
    if exc.kind == "too_big":
        return texts["file_too_big"].format(max_size=settings.max_resume_size_mb)
    return texts["file_unreadable"]


async def _accept_text_resume(
    message, db, record, candidate_id: str, text: str, settings, vacancy
) -> None:
    await repo.save_resume(
        db,
        candidate_id=candidate_id,
        session_id=record.id,
        file_name=None,
        mime_type="text/plain",
        storage_reference=None,
        extracted_text=text[:100_000],
    )
    await repo.add_audit_event(
        db, "resume_received", session_id=record.id, chars=len(text)
    )


# ---------------------------------------------------------------------------
# Вспомогательные переходы фаз
# ---------------------------------------------------------------------------


async def _ask_resume(message: Message, record, vacancy, settings) -> None:
    texts = interview_service.texts_for(vacancy)
    await message.answer(
        texts["ask_resume"].format(max_size=settings.max_resume_size_mb),
        reply_markup=kb.remove_keyboard(),
    )


async def _start_questions(message: Message, record, vacancy, settings) -> None:
    interview_service.transition(record, SessionStatus.INTERVIEWING)
    questions = await interview_service.questions_of(record)
    if not questions:
        # Сценарий без вопросов — сразу анализ
        interview_service.transition(record, SessionStatus.ANALYZING)
        await message.answer(
            interview_service.texts_for(vacancy)["analysis_running"]
        )
        return
    record.current_step = 0
    nxt = interview_service.next_question_text(record, vacancy)
    if nxt:
        await message.answer(nxt)


async def _continue_flow(message, db, record, vacancy, settings, data) -> None:
    """Продолжение возобновлённой сессии в зависимости от фазы."""
    if record.status == SessionStatus.COLLECTING_DATA:
        texts = interview_service.texts_for(vacancy)
        if record.data_step == 0:
            consent = interview_service.consent_text_for(vacancy, "")
            await message.answer(consent, reply_markup=kb.consent_keyboard())
            return
        msg = texts["ask_name"] if record.data_step == 1 else texts["ask_contact"]
        markup = kb.contact_keyboard() if record.data_step == 2 else None
        await message.answer(msg, reply_markup=markup)
        return
    if record.status == SessionStatus.COLLECTING_RESUME:
        await _ask_resume(message, record, vacancy, settings)
        return
    if record.status == SessionStatus.INTERVIEWING:
        nxt = interview_service.next_question_text(record, vacancy)
        if nxt:
            await message.answer(nxt)
        else:
            interview_service.transition(record, SessionStatus.ANALYZING)
            await db.commit()
            await _run_pipeline(message, db, record, vacancy, settings, data)
        return
    if record.status == SessionStatus.ANALYZING:
        await message.answer(interview_service.texts_for(vacancy)["analysis_running"])
        return


# ---------------------------------------------------------------------------
# Финальный пайплайн: анализ -> маршрутизация -> уведомления
# ---------------------------------------------------------------------------


async def _run_pipeline(
    message: Message, db, record: InterviewSession, vacancy: Vacancy, settings, data
) -> None:
    """Запускается в фоне: не блокирует приём других сообщений (раздел 8 TZ)."""
    bot = message.bot
    tg_chat_id = message.chat.id

    async def pipeline() -> None:
        from app.database.base import session_scope

        try:
            async with session_scope() as pipe_db:
                pipe_record = await repo.get_session_record(pipe_db, record.id)
                if pipe_record is None:
                    return
                # Защита: анализ запускается только в статусе analyzing
                if pipe_record.status != SessionStatus.ANALYZING:
                    logger.info(
                        "Пайплайн пропущен: статус %s", pipe_record.status
                    )
                    return
                pipe_vacancy = await repo.get_vacancy(pipe_db, pipe_record.vacancy_id)
                if pipe_vacancy is None:
                    return

                outcome = await ai_analysis_service.run_analysis(
                    pipe_db, pipe_record, pipe_vacancy, settings
                )
                result = await routing_service.apply_routing(
                    pipe_db, pipe_record, pipe_vacancy, outcome, settings
                )

                texts = interview_service.texts_for(pipe_vacancy)
                candidate = await repo.get_candidate(pipe_db, pipe_record.candidate_id)
                answers_rows = await repo.list_answers(pipe_db, pipe_record.id)
                answers = [(a.question_text, a.answer_text) for a in answers_rows]
                report_dict = outcome.report.model_dump() if outcome.report else {}

                # Сообщение кандидату (идемпотентно)
                msg_text = texts.get(result.message_key, "Интервью завершено.")
                await notification_service.enqueue_candidate_message(
                    pipe_db,
                    record=pipe_record,
                    telegram_chat_id=tg_chat_id,
                    text=msg_text,
                    key_suffix=result.status,
                )

                # Карточка в чат рекрутеров (только при необходимости)
                if result.notify_recruiters:
                    await notification_service.enqueue_recruiter_card(
                        pipe_db,
                        record=pipe_record,
                        vacancy=pipe_vacancy,
                        candidate_name=candidate.full_name if candidate else None,
                        contact=candidate.contact_data if candidate else None,
                        report=report_dict,
                        routing_status=result.status,
                        answers=answers,
                        score=outcome.report.score if outcome.report else None,
                        settings=settings,
                        priority=1 if result.high_priority else 0,
                    )
                elif result.status == "not_recommended":
                    # Ветка B: архив без срочного уведомления — тихая запись в журнал
                    await repo.add_audit_event(
                        pipe_db, "archived", session_id=pipe_record.id,
                        reason=result.reason,
                    )

                await repo.add_audit_event(
                    pipe_db, "routing_done", session_id=pipe_record.id,
                    status=result.status, reason=result.reason,
                )

            # Немедленная доставка очереди (воркер тоже подхватит)
            await flush_outbox(bot, settings)

        except Exception:  # noqa: BLE001
            logger.exception("Ошибка пайплайна анализа session=%s", record.id)
            try:
                async with session_scope() as err_db:
                    pipe_record = await repo.get_session_record(err_db, record.id)
                    if pipe_record is not None and pipe_record.status == SessionStatus.ANALYZING:
                        from app.database.models import SessionStatus as SS

                        interview_service.transition(pipe_record, SS.ANALYSIS_FAILED)
                        vacancy_row = await repo.get_vacancy(err_db, pipe_record.vacancy_id)
                        if vacancy_row is not None:
                            await repo.add_audit_event(
                                err_db, "pipeline_crashed",
                                session_id=pipe_record.id,
                            )
                            texts = interview_service.texts_for(vacancy_row)
                            await notification_service.enqueue_candidate_message(
                                err_db,
                                record=pipe_record,
                                telegram_chat_id=tg_chat_id,
                                text=texts["analysis_error"],
                                key_suffix="analysis_error",
                            )
                    await flush_outbox(bot, settings)
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось зафиксировать ошибку пайплайна")

    asyncio.create_task(pipeline(), name=f"pipeline-{record.id}")