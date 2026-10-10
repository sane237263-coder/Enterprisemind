from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent import attachments
from enterprise_mind.agent import lifecycle_tools as tools
from enterprise_mind.agent.models import ToolCall
from enterprise_mind.agent.service import scope_version
from enterprise_mind.agent.state import RunState
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.knowledge import confirmation
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.pending_models import PendingAction, PendingActionTarget

if TYPE_CHECKING:
    from enterprise_mind.agent.runner import Execution


def current_call(state: RunState) -> int:
    return int(
        next(
            item["tool_call_id"]
            for item in state["plan_steps"]
            if item["wave"] == state["current_wave"]
        )
    )


async def disambiguate(
    execution: "Execution",
    session: AsyncSession,
    identity: CurrentIdentity,
    name: str,
    arguments: dict[str, Any],
    ambiguity: tools.Ambiguous,
) -> RunState:
    run = await execution.guard(session, lock=True)
    now = await session.scalar(select(domain.func_clock()))
    assert isinstance(now, datetime)
    expires = now + timedelta(seconds=execution.manager.config.pending_action_ttl_seconds)
    snapshots = [await domain.snapshot(session, row, name) for row in ambiguity.rows]
    binding = {
        "binding_version": 1,
        "schema_version": 1,
        "operation_name": name,
        "actor_user_id": str(identity.user.id),
        "permission_scope_version": scope_version(identity),
        "arguments": arguments,
        "targets": snapshots,
        "expires_at": expires.isoformat(),
    }
    previous = await session.scalar(
        select(PendingAction).where(
            PendingAction.agent_run_id == run.id, PendingAction.status == "pending"
        )
    )
    if previous is None:
        action = PendingAction(
            origin="agent",
            operation_name=name,
            agent_run_id=run.id,
            actor_user_id=identity.user.id,
            idempotency_key=f"run:{run.id}:disambiguation:{run.replan_count}:{domain.digest(binding)[:20]}",
            action_type="disambiguation",
            prompt_text="找到多个有权限的目标，请选择本次要操作的一个资源。",
            bound_arguments={"binding": binding},
            binding_hash=domain.digest(binding),
            expires_at=expires,
            created_at=now,
            status="pending",
        )
        session.add(action)
        await session.flush()
        for ordinal, (row, snapshot) in enumerate(zip(ambiguity.rows, snapshots, strict=True)):
            session.add(
                PendingActionTarget(
                    pending_action_id=action.id,
                    ordinal=ordinal,
                    resource_type=ambiguity.kind,
                    resource_id=row.id,
                    display_name=snapshot["name"],
                    expected_row_version=row.row_version,
                    target_snapshot=snapshot,
                )
            )
    else:
        action = previous
    run.status, run.current_step = "waiting_confirmation", "待确认"
    return {"pending_action_id": action.id, "decision": "wait_for_user"}


async def selected_arguments(
    session: AsyncSession, run_id: int, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    action = await session.scalar(
        select(PendingAction)
        .where(
            PendingAction.agent_run_id == run_id,
            PendingAction.action_type == "disambiguation",
            PendingAction.operation_name == name,
            PendingAction.status == "accepted",
        )
        .order_by(PendingAction.id.desc())
        .limit(1)
    )
    if action and action.response and action.bound_arguments["binding"]["arguments"] == arguments:
        return {**arguments, "target": {"id": action.response["selected_target_id"]}}
    return arguments


async def guard_plan(execution: "Execution", state: RunState) -> RunState:
    steps = [s for s in state.get("plan_steps", []) if s["wave"] == state.get("current_wave", 0)]
    if not steps:
        return {"decision": "assemble_answer_context"}
    if all(s["tool_name"] == "knowledge_retrieve" for s in steps):
        return {"decision": "dispatch_wave"}
    async with execution.sessions() as session, session.begin():
        assert execution.identity is not None
        identity = await domain.write_guard(session, execution.identity)
        await execution.guard(session, lock=True)
        call = await session.get(ToolCall, current_call(state), with_for_update=True)
        assert call
        if call.status == "success":
            return {"successful_tool_call_ids": [call.id], "decision": "collect_results"}
        if call.status == "cancelled":
            call.status = "pending"
        high = await tools.high_risk(session, identity, call.tool_name, call.arguments)
        call.risk_level = "high" if high else "low"
        return {"decision": "prepare_pending" if high else "execute_mutation"}


async def prepare_pending(execution: "Execution", state: RunState) -> RunState:
    async with execution.sessions() as session, session.begin():
        assert execution.identity
        identity = await domain.write_guard(session, execution.identity)
        run = await execution.guard(session, lock=True)
        call = await session.get(ToolCall, current_call(state), with_for_update=True)
        assert call
        previous = await session.scalar(
            select(PendingAction)
            .where(PendingAction.tool_call_id == call.id)
            .order_by(PendingAction.id.desc())
            .limit(1)
        )
        if previous and previous.status == "accepted":
            return {"pending_action_id": previous.id, "decision": "execute_mutation"}
        if previous and previous.status == "pending":
            identifier = previous.id
        else:
            row = await tools.target(session, identity, call.tool_name, call.arguments)
            operation = domain.kind_of(row) + (
                ".overwrite"
                if call.tool_name == "document_overwrite"
                else ".purge"
                if call.tool_name.endswith("_purge")
                else ".delete"
            )
            if call.tool_name == "document_overwrite":
                await attachments.for_run(
                    session, identity, run.id, int(call.arguments["staged_upload_id"])
                )
            result = await confirmation.create(
                session,
                identity,
                operation,
                row.id,
                row.row_version,
                f"tool:{call.id}:confirmation:{previous.id if previous else 0}",
                arguments=call.arguments,
                run_id=run.id,
                tool_id=call.id,
                ttl_seconds=execution.manager.config.pending_action_ttl_seconds,
            )
            identifier = int(result["id"])
        run.status, run.current_step = "waiting_confirmation", "待确认"
        call.status = "waiting_confirmation"
    await execution.manager.bus.publish(
        execution.run_id, "pending_action.required", {"pending_action_id": str(identifier)}
    )
    return {"pending_action_id": identifier, "decision": "wait_for_user"}


async def wait_for_user(execution: "Execution", state: RunState) -> RunState:
    identifier = state.get("pending_action_id")
    assert identifier
    async with execution.sessions() as session:
        action = await session.get(PendingAction, identifier)
        assert action
        payload = {
            "pending_action_id": str(identifier),
            "action_type": action.action_type,
            "prompt_text": action.prompt_text,
        }
    # No transaction is held across the interrupt. The resume value is only a wake-up signal.
    interrupt(payload)
    async with execution.sessions() as session, session.begin():
        assert execution.identity
        await domain.write_guard(session, execution.identity)
        run = await execution.guard(session, lock=True)
        action = await session.get(PendingAction, identifier, with_for_update=True)
        assert action
        if action.status == "pending":
            raise APIError(ErrorCode.RUN_NOT_RESUMABLE)
        run.status = "running"
        if action.status == "expired":
            raise APIError(ErrorCode.PENDING_ACTION_EXPIRED)
        if action.status == "cancelled":
            return {
                "pending_action_id": None,
                "decision": "guard_plan" if state.get("plan_steps") else "plan",
            }
        if action.status == "rejected":
            if action.tool_call_id:
                call = await session.get(ToolCall, action.tool_call_id)
                if call:
                    call.status, call.finished_at = "cancelled", datetime.now(UTC)
            execution.clarification = "已取消本次操作，未执行修改。"
            return {
                "pending_action_id": None,
                "plan_steps": [],
                "intent_kind": "clarify",
                "requires_enterprise_facts": False,
                "decision": "assemble_answer_context",
            }
        if action.action_type == "disambiguation":
            execution.feedback = (
                "用户已选择资源 ID "
                + str((action.response or {}).get("selected_target_id"))
                + "，请据此重新规划。"
            )
            return {"pending_action_id": None, "decision": "plan"}
        return {"decision": "execute_mutation"}


async def execute_mutation(execution: "Execution", state: RunState) -> RunState:
    async with execution.sessions() as session, session.begin():
        assert execution.identity
        identity = await domain.write_guard(session, execution.identity)
        await execution.guard(session, lock=True)
        call = await session.get(ToolCall, current_call(state), with_for_update=True)
        assert call
        if call.status != "success":
            call.started_at = call.started_at or datetime.now(UTC)
            if call.risk_level == "high":
                action = await session.get(
                    PendingAction, state.get("pending_action_id"), with_for_update=True
                )
                if action is None or action.status != "accepted" or action.tool_call_id != call.id:
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
                binding = action.bound_arguments["binding"]
                now = await session.scalar(select(domain.func_clock()))
                assert isinstance(now, datetime)
                if (
                    action.expires_at <= now
                    or domain.digest(binding) != action.binding_hash
                    or binding["permission_scope_version"] != scope_version(identity)
                    or binding["arguments"]["arguments"] != call.arguments
                ):
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
                row = await tools.target(session, identity, call.tool_name, call.arguments)
                if (
                    await domain.snapshot(session, row, action.operation_name)
                    != binding["targets"][0]
                ):
                    raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)
                if call.tool_name == "document_overwrite":
                    await attachments.for_run(
                        session, identity, execution.run_id, int(call.arguments["staged_upload_id"])
                    )
                result = await domain.apply_operation(
                    session, identity, action.operation_name, row, call.arguments
                )
            else:
                result = await tools.apply_low(
                    session, identity, call.tool_name, call.arguments, run_id=execution.run_id
                )
            call.result_summary = tools.safe_result(result)
            call.status, call.finished_at = "success", datetime.now(UTC)
        return {
            "pending_action_id": None,
            "successful_tool_call_ids": [call.id],
            "decision": "collect_results",
        }
