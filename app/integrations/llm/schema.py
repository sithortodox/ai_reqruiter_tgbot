"""Схема валидации JSON-отчёта AI-аналитики (раздел 4.5 TZ)."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

RECOMMENDATIONS = ("next_stage", "reject", "manual_review")


class Skill(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    evidence: str = Field(default="", max_length=1000)
    level: Literal["junior", "middle", "senior", "unknown"] = "unknown"


class AIReportSchema(BaseModel):
    """Структура, формируемая LLM; расширена полями relevant_experience и др."""

    candidate_id: str = ""
    vacancy_id: str = ""
    score: int = Field(ge=1, le=10)
    summary: str = Field(default="", max_length=4000)
    skills: list[Skill] = Field(default_factory=list)
    relevant_experience: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    mandatory_unmet: list[str] = Field(default_factory=list)
    stop_factors: list[str] = Field(default_factory=list)
    mandatory_requirements_met: bool = True
    insufficient_data: bool = False
    recommendation: Literal["next_stage", "reject", "manual_review"]
    reasoning_summary: str = Field(default="", max_length=4000)
    confidence: Literal["low", "medium", "high"] = "medium"
    facts_to_verify: list[str] = Field(default_factory=list)

    @field_validator("score")
    @classmethod
    def _score_range(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("score должен быть в диапазоне 1..10")
        return v


class ReportValidationError(Exception):
    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.message = message
        self.details = details


_CODEBLOCK_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(content: str) -> dict:
    """Достаёт JSON из ответа модели, убирая markdown-обёртку и хвостовой мусор."""
    text = content.strip()
    text = _CODEBLOCK_RE.sub("", text).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Попытка выделить первый сбалансированный {...}
    start = text.find("{")
    if start != -1:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : idx + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
    raise ReportValidationError("В ответе модели нет валидного JSON")


def validate_report(content: str) -> AIReportSchema:
    """Полная проверка ответа LLM перед сохранением."""
    data = extract_json(content)
    try:
        report = AIReportSchema.model_validate(data)
    except ValidationError as exc:
        raise ReportValidationError(
            "Ответ LLM не соответствует схеме отчёта",
            details=str(exc)[:1500],
        ) from exc

    if report.recommendation not in RECOMMENDATIONS:
        raise ReportValidationError(
            f"Недопустимая рекомендация: {report.recommendation}"
        )
    return report