from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.service import scope_version
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion.models import StorageObject
from enterprise_mind.ingestion.service import utc
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.pending_models import PendingAction, PendingActionTarget
from enterprise_mind.presentation import format_beijing_time


def view(action: PendingAction) -> dict[str, Any]:
    result = {
        "id": str(action.id),
        "pending_action_id": str(action.id),
        "origin": action.origin,
        "action_type": action.action_type,
        "operation_name": action.operation_name,
        "status": action.status,
        "prompt_text": action.prompt_text,
        "expires_at": utc(action.expires_at),
        "resolved_at": utc(action.resolved_at),
        "result": action.response.get("result") if action.response else None,
    }
    preview = (action.bound_arguments or {}).get("binding", {}).get("preview")
    if isinstance(preview, dict):
        result["preview"] = preview
    return result


async def get(session: AsyncSession, identity: CurrentIdentity, identifier: int) -> PendingAction:
    action = await session.scalar(
        select(PendingAction)
        .where(PendingAction.id == identifier, PendingAction.actor_user_id == identity.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return action


async def create(
    session: AsyncSession,
    identity: CurrentIdentity,
    operation: str,
    identifier: int,
    expected_version: int,
    key: str,
    *,
    arguments: dict[str, Any] | None = None,
    ttl_seconds: int = 900,
    run_id: int | None = None,
    tool_id: int | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = arguments or {}
    request = {
        "operation": operation,
        "resource_id": str(identifier),
        "row_version": str(expected_version),
        "arguments": args,
    }
    fingerprint = domain.digest(
        {
            **request,
            "arguments": {
                k: v for k, v in args.items() if k != "staged_upload_id" or run_id is not None
            },
        }
    )
    previous = await session.scalar(
        select(PendingAction).where(
            PendingAction.actor_user_id == identity.user.id, PendingAction.idempotency_key == key
        )
    )
    if previous is not None:
        if previous.bound_arguments.get("request_hash") != fingerprint:
            raise APIError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
        if operation == "document.overwrite" and run_id is None:
            staged = await session.get(StorageObject, int(args["staged_upload_id"]))
            if (
                staged is not None
                and str(staged.id)
                != previous.bound_arguments["binding"]["arguments"]["arguments"]["staged_upload_id"]
            ):
                staged.retention_until = await session.scalar(select(domain.func_clock()))
        return view(previous)
    kind, verb = operation.split(".")
    root = await domain.resource(session, identity, kind, identifier, recycled=verb == "purge")
    domain.version(root, expected_version)
    snap = await domain.snapshot(session, root, operation)
    # Empty folder deletion is a low-risk action and is handled by the API's idempotent path.
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    expires = now + timedelta(seconds=ttl_seconds)
    if operation == "document.overwrite":
        staged = await session.get(
            StorageObject, int(args["staged_upload_id"]), with_for_update=True
        )
        if (
            staged is None
            or staged.status != "staged"
            or staged.content_sha256 != args["content_sha256"]
            or staged.retention_until is None
            or staged.retention_until <= now
        ):
            raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
        expires = min(expires, staged.retention_until)
    binding = {
        "binding_version": 1,
        "schema_version": 1,
        "operation_name": operation,
        "actor_user_id": str(identity.user.id),
        "permission_scope_version": scope_version(identity),
        "arguments": request,
        "targets": [snap],
        "expires_at": utc(expires),
    }
    if preview is not None:
        binding["preview"] = preview
    verbs = {"delete": "删除", "purge": "永久删除", "overwrite": "覆盖"}
    consequence = (
        "进入回收站并保留 30 天。"
        if verb == "delete" and kind != "conversation"
        else "永久删除，无法恢复。"
        if verb in {"delete", "purge"}
        else "创建新的处理代次，处理成功前继续使用旧的有效内容。"
    )
    prompt = (
        f"确认{verbs[verb]}“{snap['path']}”？影响 {snap.get('member_count', 1)} 个资源。"
        + (f"待写入文件：“{args['file_name']}”。" if verb == "overwrite" else "")
        + f"{consequence}确认有效期至 {format_beijing_time(expires)}。"
    )
    action = PendingAction(
        origin="agent" if run_id else "api",
        operation_name=operation,
        agent_run_id=run_id,
        tool_call_id=tool_id,
        actor_user_id=identity.user.id,
        idempotency_key=key,
        action_type="confirmation",
        prompt_text=prompt,
        bound_arguments={"request_hash": fingerprint, "binding": binding},
        binding_hash=domain.digest(binding),
        status="pending",
        expires_at=expires,
        created_at=now,
    )
    session.add(action)
    await session.flush()
    session.add(
        PendingActionTarget(
            pending_action_id=action.id,
            ordinal=0,
            resource_type=kind,
            resource_id=identifier,
            display_name=snap["name"],
            expected_row_version=expected_version,
            target_snapshot=snap,
        )
    )
    await session.flush()
    return view(action)


async def respond(
    session: AsyncSession,
    identity: CurrentIdentity,
    identifier: int,
    key: str,
    decision: str,
    selected_target_id: str | None = None,
    generated_copy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = await get(session, identity, identifier)
    if action.status == "expired":
        raise APIError(ErrorCode.PENDING_ACTION_EXPIRED)
    if action.status != "pending":
        if (
            action.response
            and action.response.get("idempotency_key") == key
            and action.response.get("decision") == decision
            and action.response.get("selected_target_id") == selected_target_id
        ):
            return view(action)
        raise APIError(ErrorCode.PENDING_ACTION_RESOLVED)
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    if now >= action.expires_at:
        action.status, action.resolved_at = "expired", now
        return view(action)
    binding = action.bound_arguments["binding"]
    if domain.digest(binding) != action.binding_hash or binding[
        "permission_scope_version"
    ] != scope_version(identity):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    request = binding["arguments"]
    result = None
    if action.action_type == "disambiguation":
        if decision == "accept":
            selected = next((t for t in binding["targets"] if t["id"] == selected_target_id), None)
            if selected is None:
                raise APIError(ErrorCode.PENDING_ACTION_RESPONSE_INVALID)
            if selected["resource_type"] == "database_semantic":
                # Database disambiguation targets are bounded semantic choices
                # (time range/metric/status), not rows in the knowledge
                # lifecycle domain.  Their schema and permission binding is
                # revalidated by database_execution before the choice is used.
                if action.operation_name != "database_query":
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
            else:
                root = await domain.resource(
                    session,
                    identity,
                    selected["resource_type"],
                    int(selected["id"]),
                    recycled=action.operation_name.endswith(("_restore", "_purge")),
                )
                if await domain.snapshot(session, root, action.operation_name) != selected:
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
    else:
        if selected_target_id is not None:
            raise APIError(ErrorCode.VALIDATION_ERROR)
        if action.operation_name == "document.generated_write":
            if decision == "accept":
                from enterprise_mind.generation.import_service import apply_generated_write

                result = await apply_generated_write(session, identity, action, generated_copy)
        else:
            kind, verb = action.operation_name.split(".")
            if decision == "accept":
                root = await domain.resource(
                    session, identity, kind, int(request["resource_id"]), recycled=verb == "purge"
                )
                snap = await domain.snapshot(session, root, action.operation_name)
                if snap != binding["targets"][0]:
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
                if action.origin == "api":
                    result = await domain.apply_operation(
                        session, identity, action.operation_name, root, request["arguments"]
                    )
    action.status = "accepted" if decision == "accept" else "rejected"
    action.resolved_at = now
    action.response = {
        "idempotency_key": key,
        "decision": decision,
        "selected_target_id": selected_target_id,
        "result": result,
    }
    if decision == "reject" and action.operation_name == "document.overwrite":
        staged = await session.get(StorageObject, int(request["arguments"]["staged_upload_id"]))
        if staged and staged.status == "staged":
            staged.retention_until = now
    await session.flush()
    return view(action)
