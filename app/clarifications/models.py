from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


QuestionStatus = Literal["open", "answered", "ignored", "resolved"]


class ClarificationQuestionCandidate(BaseModel):
    question_type: str
    question_text: str
    ai_rationale: str | None = None
    answer_affects_generation: bool = True
    public_safe: bool = False

