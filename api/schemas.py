"""Pydantic request/response schemas for the optional FastAPI gateway."""
from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - api.main imports this only when FastAPI is present
    BaseModel = object  # type: ignore
    def Field(default=None, **_kwargs): return default  # type: ignore


class DocumentUpload(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    sourceTier: str = "analyst"
    controlled: bool = False
    department: str = "default"


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1)
    department: str | None = None


class ActionRequest(BaseModel):
    confirmed: bool = Field(False, alias="_confirmed")

    class Config:
        extra = "allow"
        allow_population_by_field_name = True
        populate_by_name = True

    def _dump(self) -> dict[str, Any]:
        if hasattr(self, "model_dump"):
            return self.model_dump(by_alias=True)  # pydantic v2
        return self.dict(by_alias=True)            # pydantic v1

    def action_params(self) -> dict[str, Any]:
        data = self._dump()
        data.pop("_confirmed", None)
        data.pop("confirmed", None)
        return data
