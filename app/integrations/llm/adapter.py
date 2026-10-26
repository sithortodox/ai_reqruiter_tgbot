"""OpenAI-совместимый адаптер LLM (chat/completions).

Провайдер выбирается переменными окружения LLM_PROVIDER / LLM_BASE_URL /
LLM_MODEL — работает с OpenAI, DeepSeek, Together, Groq, локальными
серверами (vLLM, Ollama с OpenAI-совместимым API) и др.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging_conf import log_event

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Ошибка вызова LLM с классификацией для маршрутизации."""

    def __init__(self, kind: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind  # network | timeout | rate_limit | auth | invalid_json | http
        self.status_code = status_code
        self.message = message


@dataclass
class LLMResult:
    content: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int = 0
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


class LLMAdapter:
    """Единый интерфейс вызова LLM для модуля аналитики."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.provider = self.settings.llm_provider
        self.model = self.settings.llm_model
        self.base_url = self.settings.llm_base_url.rstrip("/")
        self.api_key = self.settings.llm_api_key
        self.timeout = self.settings.llm_timeout_seconds
        self.max_retries = self.settings.llm_max_retries
        # Демо-режим: без ключа работаем на заглушке, чтобы не блокировать разработку
        self.demo_mode = not self.api_key

    # ------------------------------------------------------------------
    async def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2500,
        json_mode: bool = True,
    ) -> LLMResult:
        """Один вызов модели с контролируемыми повторами."""
        if self.demo_mode:
            return self._demo_result(user_prompt)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # Поддерживается большинством совместимых провайдеров; при 400
            # повторяем без response_format.
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        started = time.monotonic()
        last_error: LLMError | None = None
        use_json_mode = json_mode
        attempts = 0

        for attempt in range(1, self.max_retries + 2):  # retries + 1 первая попытка
            attempts = attempt
            body = dict(payload)
            if not use_json_mode:
                body.pop("response_format", None)
            try:
                content, usage = await self._post(url, headers, body)
                duration_ms = int((time.monotonic() - started) * 1000)
                log_event(
                    logger,
                    "llm_call_ok",
                    provider=self.provider,
                    model=self.model,
                    duration_ms=duration_ms,
                    input_tokens=(usage or {}).get("prompt_tokens"),
                    output_tokens=(usage or {}).get("completion_tokens"),
                    attempts=attempt,
                )
                return LLMResult(
                    content=content,
                    provider=self.provider,
                    model=self.model,
                    input_tokens=(usage or {}).get("prompt_tokens"),
                    output_tokens=(usage or {}).get("completion_tokens"),
                    duration_ms=duration_ms,
                    attempts=attempt,
                )
            except LLMError as exc:
                last_error = exc
                # 400 на response_format — повторяем без него один раз
                if exc.kind == "http" and exc.status_code == 400 and use_json_mode:
                    use_json_mode = False
                    continue
                if exc.kind in {"auth", "invalid_json"} or attempt > self.max_retries:
                    break
                # Экспоненциальная задержка между повторами
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
                log_event(
                    logger,
                    "llm_call_retry",
                    attempt=attempt,
                    kind=exc.kind,
                    error=exc.message,
                )

        assert last_error is not None
        log_event(
            logger,
            "llm_call_failed",
            provider=self.provider,
            model=self.model,
            kind=last_error.kind,
            error=last_error.message,
            attempts=attempts,
        )
        raise last_error

    # ------------------------------------------------------------------
    async def _post(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", f"LLM API: таймаут {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError("network", f"LLM API: сетевая ошибка: {exc}") from exc

        if response.status_code == 401 or response.status_code == 403:
            raise LLMError("auth", "LLM API: неверный API-ключ", response.status_code)
        if response.status_code == 429:
            raise LLMError(
                "rate_limit", "LLM API: превышен лимит запросов", 429
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise LLMError(
                "http", f"LLM API HTTP {response.status_code}: {detail}", response.status_code
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError("invalid_json", "LLM API: неожиданный формат ответа") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("invalid_json", "LLM API: пустой контент ответа")
        return content, data.get("usage")

    # ------------------------------------------------------------------
    def _demo_result(self, user_prompt: str) -> LLMResult:
        """Демо-ответ при отсутствии API-ключа.

        Возвращает корректный JSON-отчёт на основе эвристик по ключевым
        словам, чтобы прогнать полный цикл интервью без сети. Для реального
        отбора обязателен настоящий ключ.
        """
        text = user_prompt.lower()
        strong_signals = (
            "postgres", "sql", "async", "asyncio", "fastapi", "docker",
            "pytest", "api", "лет", "коммерчес",
        )
        hits = sum(1 for s in strong_signals if s in text)
        score = max(1, min(10, 4 + hits))
        if hits >= 5:
            recommendation = "next_stage"
        elif hits <= 2:
            recommendation = "reject"
        else:
            recommendation = "manual_review"
        report = {
            "candidate_id": "demo",
            "vacancy_id": "demo",
            "score": score,
            "summary": "[ДЕМО-РЕЖИМ] Отчёт сгенерирован заглушкой без обращения к LLM. Укажите LLM_API_KEY для реального анализа.",
            "skills": [
                {"name": "Python", "evidence": "[ДЕМО] по тексту ответов", "level": "unknown"}
            ],
            "relevant_experience": ["[ДЕМО] данные не проверялись"],
            "strengths": [],
            "gaps": ["[ДЕМО] анализ не выполнялся"],
            "mandatory_unmet": [],
            "stop_factors": [],
            "mandatory_requirements_met": recommendation != "reject",
            "insufficient_data": True,
            "recommendation": recommendation,
            "reasoning_summary": "[ДЕМО-РЕЖИМ] Заглушка: LLM_API_KEY не задан.",
            "confidence": "low",
            "facts_to_verify": ["Весь отчёт требует ручной проверки (демо-режим)"],
        }
        import json as _json

        return LLMResult(
            content=_json.dumps(report, ensure_ascii=False),
            provider="demo",
            model="demo-stub",
            input_tokens=None,
            output_tokens=None,
            duration_ms=0,
            attempts=1,
        )


_adapter: LLMAdapter | None = None


def get_adapter(settings: Settings | None = None) -> LLMAdapter:
    global _adapter
    if _adapter is None:
        _adapter = LLMAdapter(settings)
    return _adapter


def reset_adapter() -> None:
    global _adapter
    _adapter = None