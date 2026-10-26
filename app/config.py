"""Конфигурация приложения: переменные окружения + валидация при запуске."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Критическая ошибка конфигурации: приложение не должно запускаться."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")
    recruiters_chat_id: str = Field(default="", alias="RECRUITERS_CHAT_ID")

    # База данных
    database_url: str = Field(
        default="sqlite+aiosqlite:///./dev.db", alias="DATABASE_URL"
    )

    # LLM
    llm_provider: str = Field(default="openai_compat", alias="LLM_PROVIDER")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    llm_base_url: str = Field(
        default="https://api.openai.com/v1", alias="LLM_BASE_URL"
    )
    llm_timeout_seconds: float = Field(default=90.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, alias="LLM_MAX_RETRIES")
    llm_max_input_chars: int = Field(default=12000, alias="LLM_MAX_INPUT_CHARS")
    llm_price_input_per_1k: float | None = Field(default=None, alias="LLM_PRICE_INPUT_PER_1K")
    llm_price_output_per_1k: float | None = Field(default=None, alias="LLM_PRICE_OUTPUT_PER_1K")

    # Интервью
    interview_timeout_minutes: int = Field(default=60, alias="INTERVIEW_TIMEOUT_MINUTES")
    interview_reminder_minutes: int = Field(default=30, alias="INTERVIEW_REMINDER_MINUTES")
    max_message_length: int = Field(default=4000, alias="MAX_MESSAGE_LENGTH")
    max_resume_size_mb: int = Field(default=10, alias="MAX_RESUME_SIZE_MB")

    # Вакансии / маршрутизация
    default_vacancy_id: str = Field(default="", alias="DEFAULT_VACANCY_ID")
    default_score_threshold: int = Field(default=7, alias="DEFAULT_SCORE_THRESHOLD")

    # Админ-панель (веб)
    admin_web_enabled: bool = Field(default=True, alias="ADMIN_WEB_ENABLED")
    admin_web_port: int = Field(default=8080, alias="ADMIN_WEB_PORT")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="admin", alias="ADMIN_PANEL_PASSWORD")
    panel_secret: str = Field(default="", alias="PANEL_SECRET")
    # Шифрование ПД в БД (контакты кандидатов)
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")

    # Логирование
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    tz_name: str = Field(default="UTC", alias="TZ_NAME")

    # Локальные пути / демо-режим
    storage_dir: str = Field(default="./storage", alias="STORAGE_DIR")

    # Персональные данные: срок хранения в днях (раздел 7 TZ)
    retention_days: int = Field(default=365, alias="RETENTION_DAYS")

    # --- Валидаторы ---
    @field_validator("default_score_threshold")
    @classmethod
    def _threshold_range(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("DEFAULT_SCORE_THRESHOLD должен быть в диапазоне 1..10")
        return v

    @field_validator("max_resume_size_mb")
    @classmethod
    def _resume_size(cls, v: int) -> int:
        if not 1 <= v <= 50:
            raise ValueError("MAX_RESUME_SIZE_MB должен быть в диапазоне 1..50")
        return v

    @field_validator("max_message_length")
    @classmethod
    def _msg_len(cls, v: int) -> int:
        if not 500 <= v <= 4096:
            raise ValueError("MAX_MESSAGE_LENGTH должен быть в диапазоне 500..4096")
        return v

    @field_validator("llm_max_retries")
    @classmethod
    def _retries(cls, v: int) -> int:
        if not 0 <= v <= 5:
            raise ValueError("LLM_MAX_RETRIES должен быть в диапазоне 0..5")
        return v

    @field_validator("retention_days")
    @classmethod
    def _retention(cls, v: int) -> int:
        if not 1 <= v <= 3650:
            raise ValueError("RETENTION_DAYS должен быть в диапазоне 1..3650")
        return v

    @property
    def admin_ids_set(self) -> set[int]:
        ids: set[int] = set()
        for raw in self.admin_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ids.add(int(raw))
            except ValueError:
                logger.warning("ADMIN_IDS: некорректный ID %r — пропущен", raw)
        return ids

    @property
    def recruiters_chat_id_value(self) -> int | None:
        raw = self.recruiters_chat_id.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("RECRUITERS_CHAT_ID: некорректный ID %r", raw)
            return None

    @property
    def panel_secret_value(self) -> str:
        """Секрет сессии панели: явный PANEL_SECRET либо вывод из токена."""
        if self.panel_secret:
            return self.panel_secret
        if self.bot_token:
            return f"panel-{self.bot_token[-12:]}"
        return "panel-insecure-dev-secret"


def require_for_bot(settings: Settings) -> None:
    """Проверка обязательных параметров перед запуском бота."""
    missing: list[str] = []
    if not settings.bot_token:
        missing.append("BOT_TOKEN")
    if not settings.admin_ids_set:
        missing.append("ADMIN_IDS")
    if not settings.recruiters_chat_id_value:
        missing.append("RECRUITERS_CHAT_ID")
    if not settings.llm_api_key:
        logger.warning(
            "LLM_API_KEY не задан: AI-аналитика будет работать в демо-режиме "
            "(имитация ответа модели). Для реального отбора ключ обязателен."
        )
    if not settings.admin_password or settings.admin_password == "admin":
        logger.warning(
            "ADMIN_PANEL_PASSWORD не задан или оставлен по умолчанию — "
            "веб-панель использует учётную запись по умолчанию. Задайте свой пароль."
        )
    if missing:
        raise ConfigError(
            "Отсутствуют обязательные параметры окружения: "
            + ", ".join(missing)
            + ". Скопируйте .env.example в .env и заполните значения."
        )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001 - показываем понятную ошибку запуска
        raise ConfigError(f"Ошибка чтения конфигурации: {exc}") from exc


def reset_settings_cache() -> None:
    """Сброс кэша настроек (используется в тестах)."""
    get_settings.cache_clear()