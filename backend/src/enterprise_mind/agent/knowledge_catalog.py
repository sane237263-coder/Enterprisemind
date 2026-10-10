"""Read-only, authorization-scoped listing of active knowledge bases."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.management_schemas import InputModel
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.security import BIGINT_MAX
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.knowledge.models import KnowledgeBase


class CatalogInput(InputModel):
    """A bounded keyset page over the caller's active knowledge-base catalog."""

    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        strict=True,
        description="本页最多返回条数，范围 1 到 100，默认 50。",
    )
    after_id: int | None = Field(
        default=None,
        ge=0,
        le=BIGINT_MAX,
        strict=True,
        description="续页时传入上一页 next_after_id；首次查询省略。",
    )


_DIRECT_LIST_QUESTION = re.compile(
    r"(?:我当前有权限的知识库有哪些|当前知识库有哪些|当前有哪些知识库|列出知识库|"
    r"有哪些知识库|我有哪些知识库|我能访问哪些知识库|我可以访问哪些知识库|"
    r"请问我现在有权限访问的知识库有哪些|列出我有权限的知识库|"
    r"列出当前可访问的知识库|列出所有我能访问的知识库)"
)
_DIRECT_CONTINUATION = re.compile(r"列出\s*ID\s+([1-9][0-9]{0,18})\s*之后的知识库", re.IGNORECASE)
_QUESTION_QUOTES = frozenset("\"'`“”‘’「」『』《》")
_TRAILING_PUNCTUATION = re.compile(r"[?？!！。．.，,；;：:]+$")
_WHITESPACE = re.compile(r"\s+")
_ID_TEXT = re.compile(r"[1-9][0-9]*")


def direct_request(question: str) -> CatalogInput | None:
    """Recognize only explicit, unquoted requests to list accessible libraries."""

    if not isinstance(question, str) or not question.strip():
        return None
    normalized = unicodedata.normalize("NFKC", question).strip()
    if any(character in normalized for character in _QUESTION_QUOTES):
        return None
    normalized = _TRAILING_PUNCTUATION.sub("", normalized).strip()
    normalized = _WHITESPACE.sub(" ", normalized)
    continuation = _DIRECT_CONTINUATION.fullmatch(normalized)
    if continuation is not None:
        after_id = int(continuation.group(1))
        if after_id <= BIGINT_MAX:
            return CatalogInput(after_id=after_id)
        return None
    if _DIRECT_LIST_QUESTION.fullmatch(normalized) is None:
        return None
    return CatalogInput()


async def read_catalog(
    session: AsyncSession,
    identity: CurrentIdentity,
    body: CatalogInput,
) -> dict[str, Any]:
    """Read one authorized page using the same active-base scope as the API."""

    body = CatalogInput.model_validate(body.model_dump())
    query = (
        select(KnowledgeBase)
        .where(
            AuthorizationService.knowledge_scope(identity),
            KnowledgeBase.id > (body.after_id or 0),
        )
        .order_by(KnowledgeBase.id)
        .limit(body.limit + 1)
    )
    rows = list((await session.scalars(query)).all())
    has_more = len(rows) > body.limit
    page = rows[: body.limit]
    return {
        "items": [{"id": str(row.id), "name": row.name, "type": row.type} for row in page],
        "count": len(page),
        "has_more": has_more,
        "next_after_id": int(page[-1].id) if has_more else None,
    }


def _safe_name(value: Any) -> str:
    text = str(value)
    visible: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if character in "\r\n\t\u2028\u2029":
            visible.append(" ")
        elif category.startswith("C"):
            continue
        else:
            visible.append(character)
    return "".join(visible).strip().replace("[", "［").replace("]", "］")


def render(result: dict[str, Any]) -> str:
    """Render an actual catalog page for the user without trusting stored names."""

    items = result.get("items")
    count = result.get("count")
    has_more = result.get("has_more")
    next_after_id = result.get("next_after_id")
    if (
        not isinstance(items, list)
        or type(count) is not int
        or count < 0
        or count > 100
        or count != len(items)
        or type(has_more) is not bool
        or (
            next_after_id is not None
            and (type(next_after_id) is not int or not 0 <= next_after_id <= BIGINT_MAX)
        )
        or (has_more and (next_after_id is None or not items))
        or (not has_more and next_after_id is not None)
    ):
        raise ValueError("invalid knowledge catalog result")

    if not items:
        return "当前范围没有可显示的知识库记录。"

    lines = [f"当前有权限访问的知识库（本页 {count} 个）："]
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "name", "type"}:
            raise ValueError("invalid knowledge catalog item")
        identifier = item.get("id")
        if not isinstance(identifier, str) or _ID_TEXT.fullmatch(identifier) is None:
            raise ValueError("invalid knowledge catalog identifier")
        if int(identifier) > BIGINT_MAX:
            raise ValueError("invalid knowledge catalog identifier")
        name_value = item.get("name")
        if not isinstance(name_value, str):
            raise ValueError("invalid knowledge catalog item")
        name = _safe_name(name_value)
        resource_type = item.get("type")
        if resource_type == "public":
            label = "企业公共库"
        elif resource_type == "department":
            label = "部门库"
        else:
            raise ValueError("invalid knowledge catalog item")
        lines.append(f"- {name}（{label}，ID：{identifier}）")

    if has_more:
        if next_after_id != int(items[-1]["id"]):
            raise ValueError("invalid knowledge catalog result")
        lines.append(f"还有更多知识库，可继续询问：列出 ID {next_after_id} 之后的知识库。")
    else:
        lines.append("本页之后没有更多知识库记录。")
    return "\n".join(lines)


def tool() -> StructuredTool:
    """Describe the read-only capability; execution belongs to the authenticated gateway."""

    async def gateway_only(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("knowledge catalog requires the authenticated Tool Gateway")

    schema = CatalogInput.model_json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "enterprise-mind://tools/knowledge_base_list/input/v1",
        }
    )
    return StructuredTool.from_function(
        name="knowledge_base_list",
        description=(
            "只读列出当前调用用户有权访问的活动知识库，按 ID 升序分页。"
            "不读取文档正文、不搜索内容、不统计文档数量，也不列回收站。"
            "limit 默认 50、最大 100；有下一页时将 next_after_id 作为 after_id 继续调用。"
            "Gateway 仅将 ID 和分页摘要交给模型；名称仅用于经过转义的直接目录回答。"
        ),
        args_schema=schema,
        coroutine=gateway_only,
    )


__all__ = ["CatalogInput", "direct_request", "read_catalog", "render", "tool"]
