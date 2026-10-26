"""Адаптер языковой модели: замена провайдера не затрагивает диалоговую логику."""

from app.integrations.llm.adapter import LLMAdapter, LLMError, LLMResult, get_adapter

__all__ = ["LLMAdapter", "LLMError", "LLMResult", "get_adapter"]