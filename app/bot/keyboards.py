"""Клавиатуры бота."""

from __future__ import annotations

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.database.models import Vacancy


def contact_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка отправки Telegram-контакта (4.2: контакт текстом или кнопкой)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить контакт", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, согласен(на)", callback_data="consent:yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="consent:no")],
        ]
    )


def vacancy_keyboard(vacancies: list[Vacancy]) -> InlineKeyboardMarkup | None:
    """Кнопки выбора вакансии, если активных несколько и параметр не задан."""
    if len(vacancies) <= 1:
        return None
    rows = [
        [InlineKeyboardButton(text=v.title, callback_data=f"vac:{v.slug}")]
        for v in vacancies[:10]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_keyboard(label: str = "⏭ Пропустить") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="skip")]]
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


HELP_TEXT = (
    "ℹ️ <b>Справка</b>\n\n"
    "Это бот первичного собеседования. Он задаёт вопросы, собирает резюме "
    "и передаёт результат аналитике.\n\n"
    "Команды:\n"
    "/start — запуск или возобновление интервью\n"
    "/cancel — отмена текущего интервью\n"
    "/help — эта справка\n\n"
    "Отвечайте на вопросы текстом. Если вопрос необязательный, можно "
    "нажать «Пропустить»."
)