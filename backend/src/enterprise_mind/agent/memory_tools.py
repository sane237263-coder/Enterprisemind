"""Agent-facing Memory tools with server-owned user and version boundaries."""

from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management_schemas import Identifier, InputModel
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.memory import service


class MemoryListInput(InputModel):
    category: Literal["preference", "work_context", "ongoing_context", "other"] | None = None


class MemorySaveInput(InputModel):
    category: Literal["preference", "work_context", "ongoing_context", "other"]
    content: str = Field(min_length=1, max_length=1000)
    subject_key: str | None = Field(default=None, max_length=300)
    memory_id: Identifier | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class MemoryDeleteInput(InputModel):
    memory_id: Identifier
    expected_revision: int | None = Field(default=None, ge=1)


SCHEMAS: dict[str, type[InputModel]] = {
    "memory_list": MemoryListInput,
    "memory_save": MemorySaveInput,
    "memory_delete": MemoryDeleteInput,
}


def exposed() -> list[StructuredTool]:
    async def gateway_only(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("requires the authenticated Tool Gateway")

    descriptions = {
        "memory_list": "查看当前用户自己的长期记忆；不能查看其他用户，也不授予权限。",
        "memory_save": (
            "在用户明确要求记住或修改长期偏好/背景时保存安全内容。不得保存凭据或企业数据副本。"
        ),
        "memory_delete": "删除当前用户明确指定的一条长期记忆；版本不一致时必须报告冲突。",
    }
    return [
        StructuredTool.from_function(
            name=name,
            description=descriptions[name],
            args_schema={
                **schema.model_json_schema(),
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"enterprise-mind://tools/{name}/input/v1",
            },
            coroutine=gateway_only,
        )
        for name, schema in SCHEMAS.items()
    ]


async def normalize(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    schema = SCHEMAS.get(name)
    if schema is None:
        raise ValueError("unavailable memory tool")
    body = schema.model_validate(arguments)
    values = body.model_dump(mode="json", exclude_unset=True)
    if name == "memory_save":
        # The model may identify a record, but owner and source message are
        # always filled by the runner, never accepted from model JSON.
        if values.get("memory_id") is not None:
            values["memory_id"] = str(values["memory_id"])
    if name == "memory_delete":
        values["memory_id"] = str(values["memory_id"])
    values["_owner_user_id"] = str(identity.user.id)
    return values


async def apply(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    values: dict[str, Any],
    *,
    source_message_id: int,
    max_chars: int = service.MAX_MEMORY_CHARS,
) -> dict[str, Any]:
    # Internal fields are produced by the trusted gateway; reject caller-made
    # owner overrides even if this function is reached outside the runner.
    if values.get("_owner_user_id") != str(identity.user.id):
        raise APIError(ErrorCode.PERMISSION_DENIED)
    if name == "memory_list":
        rows = await service.list_for_user(
            session, identity.user.id, category=values.get("category"), limit=50
        )
        return {"items": [service.memory_output(row) for row in rows[:50]]}
    if name == "memory_save":
        item, status, epoch = await service.save_for_user(
            session,
            identity.user.id,
            content=values["content"],
            category=values["category"],
            subject_key=values.get("subject_key"),
            memory_id=int(values["memory_id"]) if values.get("memory_id") else None,
            expected_revision=values.get("expected_revision"),
            origin="explicit",
            source_message_id=source_message_id,
            max_chars=max_chars,
        )
        return {**service.memory_output(item), "write_status": status, "memory_epoch": epoch}
    if name == "memory_delete":
        epoch = await service.delete_for_user(
            session,
            identity.user.id,
            int(values["memory_id"]),
            expected_revision=values.get("expected_revision"),
        )
        return {"id": str(values["memory_id"]), "deleted": True, "memory_epoch": epoch}
    raise ValueError("unavailable memory tool")


__all__ = ["SCHEMAS", "apply", "exposed", "normalize"]
