import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.agent.models import AgentRun, Conversation, Message
from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.authorization.service import AuthorizationService
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity

ACTIVE = ("pending", "running", "waiting_confirmation")
TERMINAL = ("completed", "failed", "cancelled")


def scope_version(identity: CurrentIdentity) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                identity.user.id,
                identity.user.credential_version,
                identity.user.role,
                identity.user.department_id,
                identity.user.status,
                identity.department.row_version if identity.department else None,
            ]
        ).encode()
    ).hexdigest()


async def actor(session: AsyncSession, user_id: int) -> CurrentIdentity:
    user = await session.get(User, user_id, populate_existing=True)
    if user is None:
        raise APIError(ErrorCode.AUTH_REQUIRED)
    department = await session.get(Department, user.department_id) if user.department_id else None
    identity = CurrentIdentity(user, department, user.credential_version)
    AuthorizationService.require_active(identity)
    return identity


async def conversation(
    session: AsyncSession,
    identity: CurrentIdentity,
    conversation_id: int,
    *,
    lock: bool = False,
) -> Conversation:
    query = (
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.owner_user_id == identity.user.id,
        )
        .execution_options(populate_existing=True)
    )
    item = await session.scalar(query.with_for_update() if lock else query)
    if item is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return item


async def owned_run(
    session: AsyncSession,
    identity: CurrentIdentity,
    run_id: int,
    *,
    lock: bool = False,
) -> AgentRun:
    query = (
        select(AgentRun)
        .join(Conversation)
        .where(
            AgentRun.id == run_id,
            Conversation.owner_user_id == identity.user.id,
        )
        .execution_options(populate_existing=True)
    )
    run = await session.scalar(query.with_for_update(of=AgentRun) if lock else query)
    if run is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return run


def conversation_output(item: Conversation) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "title": item.title,
        "row_version": item.row_version,
        "last_message_at": item.last_message_at.isoformat() if item.last_message_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def run_output(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "user_message_id": str(run.user_message_id),
        "assistant_message_id": str(run.assistant_message_id) if run.assistant_message_id else None,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "current_step": run.current_step,
        "step_message": run.step_message,
        "tool_call_count": run.tool_call_count,
        "replan_count": run.replan_count,
        "retry_count": run.retry_count,
        "failure_code": run.failure_code,
        "failure_message": run.failure_message,
    }


async def create_run(
    session: AsyncSession,
    identity: CurrentIdentity,
    conversation_id: int,
    content: str,
    content_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item = await conversation(session, identity, conversation_id, lock=True)
    if await session.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.conversation_id == item.id,
            AgentRun.status.in_(ACTIVE),
        )
        .limit(1)
    ):
        raise APIError(ErrorCode.RUN_ALREADY_ACTIVE)
    now = datetime.now(UTC)
    msg = Message(
        conversation_id=item.id,
        sequence=item.next_message_sequence,
        role="user",
        content_text=content,
        content_parts=content_parts,
        message_type="mixed" if content_parts else "text",
        status="completed",
        completed_at=now,
    )
    item.next_message_sequence += 1
    item.last_message_at = item.updated_at = now
    item.row_version += 1
    session.add(msg)
    await session.flush()
    run = AgentRun(
        conversation_id=item.id,
        user_message_id=msg.id,
        actor_user_id=identity.user.id,
        permission_scope_version=scope_version(identity),
    )
    session.add(run)
    await session.flush()
    return run_output(run)


async def assistant_message(session: AsyncSession, run: AgentRun) -> Message:
    if run.assistant_message_id:
        existing = await session.get(Message, run.assistant_message_id)
        assert existing is not None
        return existing
    item = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == run.conversation_id,
        )
        .with_for_update()
    )
    assert item is not None
    msg = Message(
        conversation_id=item.id,
        sequence=item.next_message_sequence,
        role="assistant",
        status="streaming",
    )
    item.next_message_sequence += 1
    session.add(msg)
    await session.flush()
    run.assistant_message_id = msg.id
    return msg


async def touch_conversation(session: AsyncSession, run: AgentRun) -> None:
    item = await session.scalar(
        select(Conversation)
        .where(Conversation.id == run.conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert item is not None
    item.last_message_at = item.updated_at = datetime.now(UTC)
    item.row_version += 1
