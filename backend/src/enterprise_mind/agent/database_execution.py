"""Database-specific gateway integration and bounded semantic disambiguation."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.models import AgentRun, Message, ToolCall
from enterprise_mind.agent.schemas import DatabaseQueryInput
from enterprise_mind.agent.service import scope_version
from enterprise_mind.agent.state import RunState
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.database.projection import authorized_schema
from enterprise_mind.database.query import QueryAmbiguity
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.knowledge import lifecycle as domain
from enterprise_mind.knowledge.pending_models import PendingAction, PendingActionTarget

if TYPE_CHECKING:
    from enterprise_mind.agent.runner import Execution


SEMANTICS = {
    "time_range": [
        ("current_month", "本自然月"),
        ("current_quarter", "本自然季度"),
        ("current_year", "本自然年"),
        ("all_time", "不限定时间"),
        ("clarify_in_message", "在新消息中补充自定义时间范围"),
    ],
    "metric": [
        ("count", "记录数量"),
        ("detail", "明细记录"),
        ("clarify_in_message", "在新消息中补充其他统计口径"),
    ],
    "status": [
        ("all_statuses", "不筛选状态"),
        ("clarify_in_message", "在新消息中补充业务状态含义"),
    ],
}
PROMPTS = {
    "relation": "存在多个授权关系，请选择要查询的表或 View。",
    "time_range": "请确定查询时间范围；如需其他区间，可取消后在新消息中补充。",
    "metric": "请确定统计口径；如需金额或其他指标，可取消后在新消息中补充。",
    "status": "状态含义尚不明确，请选择取消状态过滤，或补充业务定义。",
}


def is_continuation_request(question: str) -> bool:
    normalized = question.lower()
    return (
        any(
            marker in question
            for marker in ("下一页", "下页", "继续查询", "继续看", "更多结果", "剩余结果")
        )
        or question.strip() in {"继续", "更多", "后面的"}
        or "next page" in normalized
    )


async def validate_binding(
    session: AsyncSession, identity: CurrentIdentity, action: PendingAction
) -> None:
    binding = await authorized_schema(session, identity)
    stored = action.bound_arguments["binding"]["arguments"]
    if (
        str(binding.database_source_id) != stored["database_source_id"]
        or str(binding.schema_snapshot_id) != stored["schema_snapshot_id"]
        or binding.source_version != stored["source_version"]
        or binding.permission_digest != stored["permission_digest"]
    ):
        raise APIError(ErrorCode.PENDING_ACTION_BINDING_CHANGED)


async def semantics(execution: "Execution") -> list[str]:
    # Use user-authored intent, never earlier SQL or database-derived answer text.
    values: list[str] = []
    async with execution.sessions() as session:
        run = await session.get(AgentRun, execution.run_id)
        assert run
        current = await session.get(Message, run.user_message_id)
        assert current
        previous = list(
            reversed(
                (
                    await session.scalars(
                        select(Message.content_text)
                        .where(
                            Message.conversation_id == run.conversation_id,
                            Message.role == "user",
                            Message.sequence <= current.sequence,
                        )
                        .order_by(Message.sequence.desc())
                        .limit(8)
                    )
                ).all()
            )
        )
        values = [value for value in previous if value is not None]
        actions = list(
            (
                await session.scalars(
                    select(PendingAction)
                    .where(
                        PendingAction.agent_run_id == execution.run_id,
                        PendingAction.operation_name == "database_query",
                        PendingAction.status == "accepted",
                    )
                    .order_by(PendingAction.id)
                )
            ).all()
        )
        for action in actions:
            assert execution.identity
            try:
                await validate_binding(session, execution.identity, action)
            except APIError as error:
                if error.code == ErrorCode.PENDING_ACTION_BINDING_CHANGED:
                    # Recovered queries use the current snapshot. Stale choices are
                    # discarded and the model must disambiguate the current scope again.
                    continue
                raise
            selected = next(
                t
                for t in action.bound_arguments["binding"]["targets"]
                if t["id"] == (action.response or {}).get("selected_target_id")
            )
            if selected.get("semantic_code") == "clarify_in_message":
                raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
            values.append("用户已确认的查询语义：" + selected["name"])
    return values


async def disambiguate(execution: "Execution", ambiguity: QueryAmbiguity) -> RunState:
    async with execution.sessions() as session, session.begin():
        run = await execution.guard(session, lock=True)
        assert execution.identity
        now = await session.scalar(select(domain.func_clock()))
        assert isinstance(now, datetime)
        expires = now + timedelta(seconds=execution.manager.config.pending_action_ttl_seconds)
        if ambiguity.kind == "relation":
            choices = [
                (r.quoted_qualified_name, r.quoted_qualified_name)
                for r in ambiguity.binding.catalog.relations
                if not ambiguity.relations or r.key in ambiguity.relations
            ][:50]
        else:
            choices = SEMANTICS[ambiguity.kind]
        if not choices:
            raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
        targets = [
            {
                "id": str(index),
                "resource_type": "database_semantic",
                "name": label,
                "semantic_code": code,
            }
            for index, (code, label) in enumerate(choices, 1)
        ]
        binding = {
            "binding_version": 1,
            "schema_version": 1,
            "operation_name": "database_query",
            "actor_user_id": str(execution.identity.user.id),
            "permission_scope_version": scope_version(execution.identity),
            "arguments": {
                "database_source_id": str(ambiguity.binding.database_source_id),
                "schema_snapshot_id": str(ambiguity.binding.schema_snapshot_id),
                "source_version": ambiguity.binding.source_version,
                "permission_digest": ambiguity.binding.permission_digest,
                "clarification_kind": ambiguity.kind,
            },
            "targets": targets,
            "expires_at": expires.isoformat(),
        }
        action = await session.scalar(
            select(PendingAction).where(
                PendingAction.agent_run_id == run.id, PendingAction.status == "pending"
            )
        )
        if action is None:
            action = PendingAction(
                origin="agent",
                operation_name="database_query",
                agent_run_id=run.id,
                actor_user_id=run.actor_user_id,
                idempotency_key=f"run:{run.id}:database-disambiguation:{domain.digest(binding)[:24]}",
                action_type="disambiguation",
                prompt_text=PROMPTS[ambiguity.kind],
                bound_arguments={"binding": binding},
                binding_hash=domain.digest(binding),
                expires_at=expires,
                created_at=now,
            )
            session.add(action)
            await session.flush()
            for ordinal, target in enumerate(targets):
                session.add(
                    PendingActionTarget(
                        pending_action_id=action.id,
                        ordinal=ordinal,
                        resource_type="database_semantic",
                        resource_id=int(target["id"]),
                        display_name=target["name"],
                        target_snapshot=target,
                    )
                )
        run.status, run.current_step = "waiting_confirmation", "待确认"
        identifier = action.id
    await execution.manager.bus.publish(
        execution.run_id, "pending_action.required", {"pending_action_id": str(identifier)}
    )
    return {"pending_action_id": identifier, "decision": "wait_for_user"}


async def prepare_call(execution: "Execution", call: ToolCall) -> None:
    assert execution.identity and execution.manager.database
    if execution.database_continuation_missing:
        raise APIError(ErrorCode.QUERY_CONTEXT_EXPIRED)
    if execution.database_continuation_unavailable:
        raise APIError(ErrorCode.DATABASE_QUERY_REJECTED)
    if call.id not in execution.database_prepared:
        if execution.database_continuation is not None:
            execution.database_prepared[call.id] = replace(
                execution.database_continuation.prepared,
                page_index=execution.database_continuation.next_page_index,
            )
            return
        execution.database_prepared[call.id] = await execution.manager.database.prepare(
            execution.identity,
            DatabaseQueryInput(
                question=execution.question,
                page_size=call.arguments["page_size"],
                # Model/tool-call page offsets are never trusted for a new
                # query.  Continuation pages use the process-local context
                # selected by the runner below.
                page_index=0,
            ),
            await semantics(execution),
        )


async def execute_call(execution: "Execution", call_id: int) -> dict[str, Any]:
    assert execution.identity and execution.manager.database
    assert execution.conversation_id is not None
    answer = execution.database_answers.get(call_id)
    if answer is None:
        async with execution.sessions() as session:
            call = await session.get(ToolCall, call_id)
            assert call
        await prepare_call(execution, call)
        prepared = execution.database_prepared[call_id]
        continuation = execution.database_continuation
        if continuation is not None:
            answer = await execution.manager.database.execute_page(
                execution.identity,
                prepared,
                continuation.guarded_query,
                query_kind=continuation.query_kind,
                conditions_summary=continuation.conditions_summary,
            )
        else:
            answer = await execution.manager.database.execute(execution.identity, prepared)
        execution.database_answers[call_id] = answer
        if continuation is not None:
            context = execution.manager.database_queries.advance(
                continuation,
                owner_user_id=execution.identity.user.id,
                conversation_id=execution.conversation_id,
                answer=answer,
            )
        else:
            context = execution.manager.database_queries.start(
                owner_user_id=execution.identity.user.id,
                conversation_id=execution.conversation_id,
                answer=answer,
            )
        execution.database_query_ids[call_id] = context.query_id
        async with execution.sessions() as session, session.begin():
            await execution.guard(session, lock=True)
            call = await session.get(ToolCall, call_id)
            assert call
            call.arguments = prepared.controls()
            call.arguments_hash = domain.digest(call.arguments)
            call.result_summary = answer.summary()
    await execution.manager.database.verify(execution.identity, answer)
    return answer.summary()
