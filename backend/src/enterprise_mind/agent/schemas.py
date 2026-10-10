import json
import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from enterprise_mind.agent.client_actions import ClientActionRequest
from enterprise_mind.api.management_schemas import Identifier, InputModel
from enterprise_mind.identity.security import normalize_name
from enterprise_mind.rag.repository import Scope


class ConversationCreate(InputModel):
    title: str = Field(default="新会话", min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty title")
        return value.strip()


class ConversationRename(ConversationCreate):
    row_version: int = Field(ge=1)


class MessageCreate(InputModel):
    content_text: str = Field(default="", max_length=16000)
    staged_upload_ids: list[Identifier] = Field(
        default_factory=list,
        max_length=10,
        json_schema_extra={"uniqueItems": True},
        # Preserve the pre-attachment fingerprint for text-only idempotent requests.
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def nonempty(self) -> "MessageCreate":
        if not self.content_text and not self.staged_upload_ids:
            raise ValueError("message requires text or an attachment")
        return self

    @field_validator("staged_upload_ids")
    @classmethod
    def unique_attachments(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("duplicate attachment")
        return values

    @field_validator("content_text")
    @classmethod
    def no_credentials(cls, value: str) -> str:
        if re.search(
            r"(?i)(?:password|密码|api[_ -]?key|access[_ -]?token|secret)\s*[:=：]\s*\S+"
            r"|Bearer\s+[A-Za-z0-9._-]+|sk-[A-Za-z0-9_-]{16,}"
            r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            value,
        ):
            raise ValueError("empty input or credentials")
        return value.strip()


class RetrievalInput(InputModel):
    question: str = Field(
        min_length=1, max_length=4000, description="要检索的内容问题；文件名和 ID 放入范围字段。"
    )
    knowledge_base_ids: list[Identifier] | None = Field(default=None, max_length=100)
    folder_ids: list[Identifier] | None = Field(default=None, max_length=100)
    document_ids: list[Identifier] | None = Field(
        default=None,
        max_length=100,
        description="已知的数字文档 ID 字符串，不能填文件名或猜测 ID。",
    )
    document_names: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(
        default=None,
        max_length=100,
        description="用户指定的完整文件名（含扩展名），精确匹配当前有权访问的文档；与其他范围取交集。",
    )
    extensions: (
        list[
            Literal[
                "pdf",
                "md",
                "markdown",
                "txt",
                "csv",
                "doc",
                "docx",
                "ppt",
                "pptx",
                "xls",
                "xlsx",
                "png",
                "jpg",
                "jpeg",
                "webp",
            ]
        ]
        | None
    ) = Field(default=None, max_length=15)

    @field_validator("document_names")
    @classmethod
    def valid_document_names(cls, values: list[str] | None) -> list[str] | None:
        return (
            sorted({normalize_name(value, max_length=255) for value in values})
            if values is not None
            else None
        )

    def scope(self) -> Scope:
        def identifiers(values: list[str] | None) -> tuple[int, ...] | None:
            return tuple(sorted({int(item) for item in values})) if values is not None else None

        return Scope(
            identifiers(self.knowledge_base_ids),
            identifiers(self.folder_ids),
            identifiers(self.document_ids),
            tuple(sorted(set(self.extensions))) if self.extensions is not None else None,
            tuple(self.document_names) if self.document_names is not None else None,
        )


class ProposedTool(InputModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=12)


class DatabaseQueryInput(InputModel):
    question: str = Field(min_length=1, max_length=4000, repr=False)
    page_size: int = Field(default=100, ge=1, le=100, strict=True)
    # Kept for checkpoint/model compatibility; the runner ignores caller
    # values and always starts a fresh query at page zero.
    page_index: int = Field(default=0, ge=0, le=21474836, strict=True)


class DatabasePageRequest(InputModel):
    """Opaque process-local continuation binding; SQL and offsets are absent."""

    query_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    result_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class Plan(InputModel):
    intent_kind: Literal["knowledge", "database", "general", "clarify", "unsupported"]
    requires_enterprise_facts: bool
    tools: list[ProposedTool] = Field(default_factory=list, max_length=12)
    clarification: str | None = Field(default=None, max_length=500)
    client_action: ClientActionRequest | None = None

    @field_validator("client_action", mode="before")
    @classmethod
    def decode_client_action(cls, value: Any) -> Any:
        # Some function-calling providers serialize a nested object as a JSON string.
        # Decode once; the closed action union still validates every field below.
        if isinstance(value, str):
            if len(value) > 4000:
                raise ValueError("client action too large")
            return json.loads(value)
        return value

    @model_validator(mode="after")
    def graph(self) -> "Plan":
        if self.client_action is not None and (
            self.tools or self.requires_enterprise_facts or self.intent_kind != "general"
        ):
            raise ValueError("client navigation must be an independent general plan")
        keys = [item.key for item in self.tools]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate plan step")
        if sum(item.tool_name == "database_query" for item in self.tools) > 1:
            raise ValueError("multiple database queries in one plan")
        done: set[str] = set()
        for _ in self.tools:
            for item in self.tools:
                if set(item.depends_on) <= done:
                    done.add(item.key)
        if done != set(keys):
            raise ValueError("cyclic or unresolved dependency")
        if self.intent_kind in {"clarify", "unsupported"} and self.tools:
            raise ValueError("clarification must not execute tools")
        return self

    def waves(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for _ in self.tools:
            for item in self.tools:
                if all(key in result for key in item.depends_on):
                    result[item.key] = max((result[key] + 1 for key in item.depends_on), default=0)
        return result
