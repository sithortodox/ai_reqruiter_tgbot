"""Настройка структурированного журналирования."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Маскируемые ключи: в логи не должны попадать секреты.
_SECRET_MARKERS = ("token", "api_key", "apikey", "password", "secret", "authorization")


def _mask(value: str) -> str:
    if len(value) > 8:
        return f"{value[:4]}***{value[-4:]}"
    return "***"


class JsonFormatter(logging.Formatter):
    """Структурированные JSON-логи для машиночитаемого анализа."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "event_data", None)
        if isinstance(extra, dict):
            payload["data"] = _scrub(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _scrub(data: Any) -> Any:
    """Убирает/маскирует секреты в структурированных данных события."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
                out[key] = _mask(str(value))
            else:
                out[key] = _scrub(value)
        return out
    if isinstance(data, (list, tuple)):
        return [_scrub(v) for v in data]
    return data


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]

    # Гасим избыточный вывод сторонних библиотек.
    for noisy in ("httpx", "httpcore", "aiosqlite", "uvicorn.access", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, **data: Any) -> None:
    """Единая точка регистрации ключевых событий (AuditLog продублирует их в БД)."""
    logger.info(event, extra={"event_data": {"event": event, **data}})