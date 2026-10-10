from typing import Literal

from pydantic import Field

from enterprise_mind.api.management_schemas import InputModel

MemoryCategory = Literal["preference", "work_context", "ongoing_context", "other"]


class MemoryCreate(InputModel):
    category: MemoryCategory
    content: str = Field(min_length=1, max_length=1000)
    subject_key: str | None = Field(default=None, max_length=300)


class MemoryUpdate(InputModel):
    content: str = Field(min_length=1, max_length=1000)
    category: MemoryCategory | None = None
    subject_key: str | None = Field(default=None, max_length=300)
    expected_revision: int = Field(ge=1)


class MemoryDelete(InputModel):
    expected_revision: int | None = Field(default=None, ge=1)


__all__ = ["MemoryCategory", "MemoryCreate", "MemoryDelete", "MemoryUpdate"]
