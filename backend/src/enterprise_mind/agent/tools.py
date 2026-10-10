from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import StructuredTool

from enterprise_mind.agent.schemas import DatabaseQueryInput, RetrievalInput


def retrieval_tool(
    execute: Callable[[RetrievalInput], Awaitable[dict[str, Any]]],
) -> StructuredTool:
    async def run(**kwargs: Any) -> dict[str, Any]:
        return await execute(RetrievalInput.model_validate(kwargs))

    return StructuredTool.from_function(
        name="knowledge_retrieve",
        description="根据内容问题检索当前可访问的知识库原文。用户只给文件名时使用 document_names，"
        "已知数字 ID 才使用 document_ids；不得把文件名放入 ID 或删除指定范围。"
        "可限定库、文件夹、文档和格式；范围只取交集，未指定的范围省略或为 null。",
        args_schema=RetrievalInput,
        coroutine=run,
    )


def database_tool() -> StructuredTool:
    async def internal_only(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Database tools execute through the private gateway only")

    return StructuredTool.from_function(
        name="database_query",
        description="根据当前自然语言问题只读查询授权企业数据库；可指定每页1到100行，下一页由服务端上下文控制。"
        "不得提供SQL或页码。缺少关键时间、口径或状态条件时先消歧。",
        args_schema=DatabaseQueryInput,
        coroutine=internal_only,
    )
