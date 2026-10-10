from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: Literal["preference", "work_context", "ongoing_context", "other"]
    subject_key: str | None = Field(default=None, max_length=300)
    content: str = Field(min_length=1, max_length=1000)


class MemoryCandidateSet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=3)


__all__ = ["MemoryCandidate", "MemoryCandidateSet"]
