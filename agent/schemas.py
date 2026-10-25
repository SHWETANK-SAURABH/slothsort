"""Pydantic schemas for model output and the reviewable plan."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from config import ALLOWED_CATEGORIES


class ModelSuggestion(BaseModel):
    """One item of raw model output. Still untrusted after parsing."""

    id: str = Field(min_length=1, max_length=20)
    category: str
    suggested_filename: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=600)
    confidence: float = Field(default=0.5)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> float:
        try:
            return max(0.0, min(1.0, float(v)))  # not calibrated; only used for routing
        except (TypeError, ValueError):
            return 0.0


class BatchResponse(BaseModel):
    items: list[ModelSuggestion]


def response_json_schema() -> dict:
    """JSON schema passed to Ollama's structured-output ``format`` option."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string", "enum": list(ALLOWED_CATEGORIES)},
                        "suggested_filename": {"type": "string"},
                        "reason": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["id", "category", "suggested_filename", "reason", "confidence"],
                },
            }
        },
        "required": ["items"],
    }


class PlanItem(BaseModel):
    """A reviewable suggestion for one file. Approval is a separate, explicit step."""

    file_id: str
    source_rel: str
    source_abs: str
    sha256: str
    size: int
    mtime_ns: int
    ext: str
    category: str
    proposed_filename: str
    destination_rel: str = ""
    destination_abs: str = ""
    reason: str = ""
    confidence: float = 0.0
    origin: str = "ai"  # "ai" | "rules"
    needs_review: bool = False
    validation_status: str = "valid"  # "valid" | "invalid" | "unchanged"
    validation_message: str = ""
    duplicate_group: int | None = None
    selected: bool = False
    rejected: bool = False
