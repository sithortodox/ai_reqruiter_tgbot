"""Модуль обработки файлов: приём и извлечение текста из резюме (раздел 4.4)."""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
ALLOWED_MIMES = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
    ".txt": {"text/plain", "text/utf-8", ""},
}

# Максимальный объём текста, сохраняемого/передаваемого дальше
MAX_TEXT_CHARS = 100_000


class ResumeError(Exception):
    """Ошибка обработки резюме с типом для выбора сообщения кандидату."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # unsupported | too_big | unreadable


def validate_file(file_name: str, mime_type: str | None, size: int, settings: Settings) -> None:
    """Проверка расширения, MIME-типа и размера."""
    ext = Path(file_name or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ResumeError("unsupported")
    max_bytes = settings.max_resume_size_mb * 1024 * 1024
    if size > max_bytes:
        raise ResumeError("too_big")
    if size <= 0:
        raise ResumeError("unreadable")
    allowed_mimes = ALLOWED_MIMES.get(ext, set())
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime and allowed_mimes and mime not in allowed_mimes:
        # Telegram часто указывает application/octet-stream — доверяем расширению
        if mime != "application/octet-stream":
            raise ResumeError("unsupported")


def extract_text(file_name: str, data: bytes) -> str:
    """Извлечение текста из PDF / DOCX / TXT. Кидает ResumeError при сбое."""
    ext = Path(file_name or "").suffix.lower()
    try:
        if ext == ".txt":
            return _from_txt(data)
        if ext == ".pdf":
            return _from_pdf(data)
        if ext == ".docx":
            return _from_docx(data)
    except ResumeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ошибка извлечения текста из %s: %s", file_name, exc)
        raise ResumeError("unreadable") from exc
    raise ResumeError("unsupported")


def _from_txt(data: bytes) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ResumeError("unreadable")
    if not text.strip():
        raise ResumeError("unreadable")
    return text[:MAX_TEXT_CHARS]


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            if extracted:
                parts.append(extracted)
        text = "\n".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF не удалось прочитать: %s", exc)
        raise ResumeError("unreadable") from exc

    if not text:
        # Скан без текстового слоя — требуется OCR (включается конфигом)
        raise ResumeError("unreadable")
    return text[:MAX_TEXT_CHARS]


def _from_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs if p.text]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text:
                        parts.append(cell.text)
        text = "\n".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DOCX не удалось прочитать: %s", exc)
        raise ResumeError("unreadable") from exc
    if not text:
        raise ResumeError("unreadable")
    return text[:MAX_TEXT_CHARS]


def save_to_storage(data: bytes, file_name: str, settings: Settings) -> str:
    """Сохраняет исходник в storage/ и возвращает ссылку (безопасное имя)."""
    storage = Path(settings.storage_dir) / "resumes"
    storage.mkdir(parents=True, exist_ok=True)
    ext = Path(file_name or "").suffix.lower() or ".bin"
    safe_name = f"{uuid.uuid4().hex}{ext}"
    path = storage / safe_name
    path.write_bytes(data)
    return str(path)


def truncate_for_llm(text: str, limit: int) -> str:
    """Ограничение объёма текста резюме для передачи в LLM."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[текст резюме обрезан по лимиту]"


def sanitize_for_prompt(text: str, limit: int = 12_000) -> str:
    """Экранирование недоверенного текста (защита от prompt injection).

    Резюме и ответы — данные, а не инструкции: обрамляем маркерами и
    убираем управляющие последовательности, которые могут имитировать
    разметку сообщений модели.
    """
    import re

    cleaned = text.replace("\x00", "")
    # Убираем упоминания ролей/инструкций в начале строк
    cleaned = re.sub(
        r"(?im)^\s*(system|assistant|user)\s*:",
        lambda m: f"[{m.group(1).upper()} в тексте]",
        cleaned,
    )
    cleaned = cleaned[:limit]
    return f"<<<ДАННЫЕ_КАНДИДАТА>>>\n{cleaned}\n<<<КОНЕЦ_ДАННЫХ>>>"