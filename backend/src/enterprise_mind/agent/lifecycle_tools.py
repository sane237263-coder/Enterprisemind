"""Fixed schemas and server-side resolution for the implemented lifecycle tools."""

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent import attachments, management_tools
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.api.management_schemas import (
    Identifier,
    InputModel,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    Name,
    reject_nulls,
)
from enterprise_mind.identity import management
from enterprise_mind.identity.security import normalize_name
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import Document, Folder
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.models import KnowledgeBase


class Selector(InputModel):
    id: Identifier | None = None
    name: str | None = Field(default=None, min_length=1, max_length=500)
    path: str | None = Field(default=None, min_length=1, max_length=2000)
    knowledge_base_id: Identifier | None = None

    @model_validator(mode="after")
    def identifiable(self) -> "Selector":
        if not any((self.id, self.name, self.path)):
            raise ValueError("resource selector required")
        return self


class TargetInput(InputModel):
    target: Selector


class DocumentChanges(InputModel):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=4000)
    folder_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "DocumentChanges":
        reject_nulls(self, {"description", "folder_id"})
        return self


class FolderChanges(InputModel):
    name: Name | None = None
    parent_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "FolderChanges":
        reject_nulls(self, {"parent_id"})
        return self


class BaseChanges(InputModel):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=4000)
    department_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "BaseChanges":
        reject_nulls(self, {"description"})
        return self


class DocumentInput(TargetInput):
    changes: DocumentChanges


class FolderInput(TargetInput):
    changes: FolderChanges


class BaseInput(TargetInput):
    changes: BaseChanges


class RestoreTool(TargetInput):
    name: Name | None = None
    parent_id: Identifier | None = None


class FolderCreateTool(InputModel):
    knowledge_base_id: Identifier
    parent_id: Identifier | None = None
    name: Name


class UploadFileTool(InputModel):
    staged_upload_id: Identifier
    name: Name | None = None
    description: str | None = Field(default=None, max_length=4000)


class DocumentUploadTool(InputModel):
    knowledge_base_id: Identifier
    folder_id: Identifier | None = None
    files: list[UploadFileTool] = Field(min_length=1, max_length=attachments.MAX_ATTACHMENTS)

    @model_validator(mode="after")
    def unique_files(self) -> "DocumentUploadTool":
        identifiers = [file.staged_upload_id for file in self.files]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate attachment")
        return self


class DocumentOverwriteTool(TargetInput):
    staged_upload_id: Identifier


SCHEMAS: dict[str, type[InputModel]] = {
    **management_tools.SCHEMAS,
    "document_upload": DocumentUploadTool,
    "document_overwrite": DocumentOverwriteTool,
    "document_update": DocumentInput,
    "document_delete": TargetInput,
    "document_restore": RestoreTool,
    "document_purge": TargetInput,
    "document_retry_processing": TargetInput,
    "folder_create": FolderCreateTool,
    "folder_update": FolderInput,
    "folder_delete": TargetInput,
    "folder_restore": RestoreTool,
    "folder_purge": TargetInput,
    "knowledge_base_create": KnowledgeBaseCreate,
    "knowledge_base_update": BaseInput,
    "knowledge_base_delete": TargetInput,
    "knowledge_base_restore": RestoreTool,
    "knowledge_base_purge": TargetInput,
}


def exposed(identity: CurrentIdentity) -> list[StructuredTool]:
    async def gateway_only(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("requires the authenticated Tool Gateway")

    return [
        StructuredTool.from_function(
            name=name,
            description=management_tools.DESCRIPTIONS.get(name)
            or (
                "将用户已绑定到当前会话消息的附件保存到目标知识库，返回每个文件的处理任务或同名冲突。"
                "只能使用服务端提供的 staged_upload_id。同名冲突时询问用户覆盖或改名。"
                if name == "document_upload"
                else "使用当前会话的一个附件覆盖唯一目标文档，必须等待用户二次确认。"
                "不能使用文件路径、URL、正文或自造附件 ID。"
                if name == "document_overwrite"
                else "管理当前有权限的知识库内容。删除、覆盖和永久删除必须经用户确认；"
                "不得自动代替用户确认。"
            ),
            args_schema={
                **schema.model_json_schema(),
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"enterprise-mind://tools/{name}/input/v1",
            },
            coroutine=gateway_only,
        )
        for name, schema in SCHEMAS.items()
        if identity.user.role == "admin"
        or (not name.startswith("knowledge_base_") and name not in management_tools.SCHEMAS)
    ]


class Ambiguous(Exception):
    def __init__(self, kind: str, rows: list[domain.Resource]) -> None:
        self.kind, self.rows = kind, rows
        super().__init__("resource requires disambiguation")


async def resolve(
    session: AsyncSession, identity: CurrentIdentity, kind: str, target: Selector, recycled: bool
) -> domain.Resource:
    if target.id:
        rows = [await domain.resource(session, identity, kind, int(target.id), recycled=recycled)]
    else:
        model: Any = {"document": Document, "folder": Folder, "knowledge_base": KnowledgeBase}[kind]
        query = select(model).where(
            model.deleted_at.is_not(None) if recycled else model.deleted_at.is_(None)
        )
        if target.name:
            query = query.where(model.name_key == normalize_name(target.name, max_length=255))
        elif target.path:
            query = query.where(
                model.name_key
                == normalize_name(target.path.rstrip("/").split("/")[-1], max_length=255)
            )
        if kind != "knowledge_base":
            query = query.join(KnowledgeBase, KnowledgeBase.id == model.knowledge_base_id)
        if identity.user.role != "admin":
            query = query.where(
                (KnowledgeBase.type == "public")
                | (KnowledgeBase.department_id == identity.user.department_id)
            )
        if not recycled:
            query = query.where(KnowledgeBase.deleted_at.is_(None))
        if target.knowledge_base_id:
            query = query.where(KnowledgeBase.id == int(target.knowledge_base_id))
        rows = list((await session.scalars(query.order_by(model.id))).all())
    matches: list[domain.Resource] = []
    for row in rows:
        assert isinstance(row, KnowledgeBase | Folder | Document)
        base_id = row.id if isinstance(row, KnowledgeBase) else row.knowledge_base_id
        if target.knowledge_base_id and base_id != int(target.knowledge_base_id):
            continue
        if target.name and row.name_key != normalize_name(target.name, max_length=255):
            continue
        if target.path and (await domain.path_of(session, row)).rstrip("/") != target.path.rstrip(
            "/"
        ):
            continue
        matches.append(row)
        if len(matches) > 10:
            raise ValueError("too many candidates; narrow the knowledge base or folder")
    if not matches:
        raise ValueError("no authorized matching resource; request a more precise selector")
    if len(matches) > 1:
        raise Ambiguous(kind, matches)
    return matches[0]


async def normalize(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    arguments: dict[str, Any],
    *,
    run_id: int | None = None,
) -> dict[str, Any]:
    if name in management_tools.SCHEMAS:
        return await management_tools.normalize(session, identity, name, arguments)
    schema = SCHEMAS.get(name)
    if schema is None or (name.startswith("knowledge_base_") and identity.user.role != "admin"):
        raise ValueError("unavailable tool")
    body = schema.model_validate(arguments)
    values = body.model_dump(mode="json", exclude_unset=True)
    if name in {"document_upload", "document_overwrite"}:
        if run_id is None:
            raise ValueError("file tools require a bound conversation run")
        if isinstance(body, DocumentUploadTool):
            from enterprise_mind.ingestion.service import upload_scope

            await upload_scope(
                session,
                identity,
                int(body.knowledge_base_id),
                int(body.folder_id) if body.folder_id else None,
            )
            for item in values["files"]:
                obj = await attachments.for_run(
                    session, identity, run_id, int(item["staged_upload_id"])
                )
                item.update(attachments.metadata(obj))
                # Same display-name rules as direct uploads, before any mutation.
                name_value = item.get("name") or obj.attachment_file_name
                normalize_name(name_value or "", max_length=255)
                if any(char in (name_value or "") for char in ("/", "\\")):
                    raise ValueError("invalid document name")
            return values
        assert isinstance(body, DocumentOverwriteTool)
        obj = await attachments.for_run(session, identity, run_id, int(body.staged_upload_id))
        values.update(attachments.metadata(obj))
    if name.endswith("_create"):
        return values
    assert isinstance(body, TargetInput)
    kind = "knowledge_base" if name.startswith("knowledge_base_") else name.split("_")[0]
    row = await resolve(session, identity, kind, body.target, name.endswith(("_restore", "_purge")))
    values["target"] = {"id": str(row.id)}
    values["row_version"] = row.row_version
    return values


async def target(
    session: AsyncSession, identity: CurrentIdentity, name: str, values: dict[str, Any]
) -> domain.Resource:
    kind = "knowledge_base" if name.startswith("knowledge_base_") else name.split("_")[0]
    row = await domain.resource(
        session,
        identity,
        kind,
        int(values["target"]["id"]),
        recycled=name.endswith(("_restore", "_purge")),
    )
    domain.version(row, values["row_version"])
    return row


async def high_risk(
    session: AsyncSession, identity: CurrentIdentity, name: str, values: dict[str, Any]
) -> bool:
    if name in management_tools.SCHEMAS:
        return False
    if name == "folder_delete":
        return len(await domain.members(session, await target(session, identity, name, values))) > 1
    return name.endswith(("_delete", "_purge", "_overwrite"))


async def apply_low(
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    values: dict[str, Any],
    *,
    run_id: int | None = None,
) -> dict[str, Any]:
    if name in management_tools.SCHEMAS:
        return await management_tools.apply(session, identity, name, values)
    if name == "document_upload":
        if run_id is None:
            raise ValueError("file tools require a bound conversation run")
        return await attachments.consume_upload(session, identity, run_id, values)
    if name == "knowledge_base_create":
        return await management.create_knowledge_base(
            session, KnowledgeBaseCreate.model_validate(values)
        )
    if name == "folder_create":
        return await domain.create_folder(
            session, identity, int(values["knowledge_base_id"]), values
        )
    row = await target(session, identity, name, values)
    kind = domain.kind_of(row)
    if name.endswith("_update"):
        changes = {**values["changes"], "row_version": values["row_version"]}
        if kind == "knowledge_base":
            return (
                await management.update_knowledge_base(
                    session, identity, row.id, KnowledgeBaseUpdate.model_validate(changes)
                )
            ).model_dump(mode="json")
        return await domain.update_resource(session, identity, kind, row.id, changes)
    if name.endswith("_restore"):
        return await domain.restore(
            session, identity, kind, row.id, {k: v for k, v in values.items() if k != "target"}
        )
    if name == "document_retry_processing":
        assert isinstance(row, Document)
        return await domain.retry_processing(session, row)
    if name == "folder_delete" and len(await domain.members(session, row)) == 1:
        return await domain.soft_delete(session, row)
    raise APIError(ErrorCode.VALIDATION_ERROR)


def safe_result(result: dict[str, Any]) -> dict[str, Any]:
    if "items" in result:
        allowed = {
            "staged_upload_id",
            "name",
            "status",
            "document_id",
            "processing_task_id",
            "error_code",
        }
        return {
            "items": [{k: v for k, v in item.items() if k in allowed} for item in result["items"]]
        }
    return {
        k: v
        for k, v in result.items()
        if k
        in {
            "id",
            "name",
            "username",
            "role",
            "department",
            "row_version",
            "status",
            "scope",
            "affected_count",
            "processing_task_id",
        }
    }


def receipt(name: str, result: dict[str, Any] | None) -> str:
    """Committed operation acknowledgements never rely on retrieved document text."""
    result = result or {}
    resource, _, action = name.rpartition("_")
    label = {
        "department": "部门",
        "user": "用户",
        "knowledge_base": "知识库",
        "folder": "文件夹",
        "document": "文档",
        "conversation": "会话",
    }.get(resource, "资源")
    display = result.get("name") or result.get("username")
    target = f"{label}“{display}”" if display else label
    if name == "document_upload":
        items = result.get("items", [])
        accepted = sum(item.get("status") == "accepted" for item in items)
        conflicts = sum(item.get("status") == "name_conflict" for item in items)
        return f"已提交 {accepted} 个文件的处理任务，尚不代表处理完成。" + (
            f"另有 {conflicts} 个同名文件尚未保存，请选择覆盖或改名。" if conflicts else ""
        )
    if action == "overwrite" or name == "document_retry_processing":
        return "文档处理任务已提交，尚不代表处理完成。"
    if action == "purge":
        return f"{target}的永久删除任务已提交，存储清理将在后台完成。"
    verb = {
        "create": "已创建",
        "update": "已更新",
        "restore": "已恢复",
        "delete": "已删除" if resource in {"department", "conversation"} else "已移入回收站：",
    }
    return f"{verb.get(action, '已完成操作：')}{target}。"
