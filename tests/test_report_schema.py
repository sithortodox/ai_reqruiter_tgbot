"""Валидация JSON-отчёта LLM: схема, извлечение JSON, ошибки (раздел 4.5 TZ)."""

from __future__ import annotations

import json

import pytest

from app.integrations.llm.schema import (
    AIReportSchema,
    ReportValidationError,
    extract_json,
    validate_report,
)


def valid_report(**overrides) -> dict:
    data = {
        "candidate_id": "c1",
        "vacancy_id": "v1",
        "score": 8,
        "summary": "Сильный middle-разработчик",
        "skills": [{"name": "Python", "evidence": "4 года", "level": "middle"}],
        "relevant_experience": ["FinTech"],
        "strengths": ["SQL"],
        "gaps": ["Нет опыта K8s"],
        "mandatory_unmet": [],
        "stop_factors": [],
        "mandatory_requirements_met": True,
        "insufficient_data": False,
        "recommendation": "next_stage",
        "reasoning_summary": "Соответствует",
        "confidence": "high",
        "facts_to_verify": [],
    }
    data.update(overrides)
    return data


def test_valid_report_passes():
    report = validate_report(json.dumps(valid_report(), ensure_ascii=False))
    assert isinstance(report, AIReportSchema)
    assert report.score == 8
    assert report.recommendation == "next_stage"


def test_markdown_wrapped_json_extracted():
    content = "```json\n" + json.dumps(valid_report()) + "\n```"
    report = validate_report(content)
    assert report.score == 8


def test_json_with_surrounding_noise_extracted():
    content = 'Вот отчёт:\n{"score": 5, "recommendation": "reject"}\nГотово.'
    data = extract_json(content)
    assert data["score"] == 5


def test_invalid_recommendation_rejected():
    with pytest.raises(ReportValidationError):
        validate_report(json.dumps(valid_report(recommendation="hire_now")))


def test_score_out_of_range_rejected():
    with pytest.raises(ReportValidationError):
        validate_report(json.dumps(valid_report(score=11)))


def test_missing_required_fields_rejected():
    with pytest.raises(ReportValidationError):
        validate_report(json.dumps({"summary": "нет score"}))


def test_not_json_rejected():
    with pytest.raises(ReportValidationError):
        validate_report("не json вообще")


def test_list_response_rejected():
    """Ответ-массив не является отчётом."""
    with pytest.raises(ReportValidationError):
        validate_report("[1, 2, 3]")