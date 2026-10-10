"""Small, user-scoped long-term memory service.

The service owns the user/epoch/version boundary.  Model calls happen outside
transactions; only the short merge/delete transaction locks the user's state
row.  Memory is preference/context data, never an authorization source.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.memory.models import Memory, MemoryState

MEMORY_CATEGORIES = {"preference", "work_context", "ongoing_context", "other"}
MEMORY_ORIGINS = {"explicit", "manual", "automatic"}
MAX_MEMORY_CHARS = 1000
MAX_SELECT_CANDIDATES = 50
MAX_INJECT_ITEMS = 10
MAX_INJECT_CHARS = 6000

_SECRET_PATTERNS = (
    # Stop at sentence punctuation so a credential at the start of a user
    # message does not redact the safe prose that follows it.
    re.compile(
        r"(?i)(?:password|密码|token|api[_ -]?key|access[_ -]?token|secret|密钥)\s*[:=：]\s*"
        r'[^\s,，。；;、!?！？()（）\[\]{}【】<>《》"“”‘’]+'
    ),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(?:postgres(?:ql)?|mysql|redis)://[^\s,，。；;、!?！？()（）\[\]{}【】<>《》]+"
    ),
    re.compile(
        r"(?is)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)"
    ),
)


def normalized_content(value: str, *, max_chars: int = MAX_MEMORY_CHARS) -> str:
    if not isinstance(value, str):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    content = " ".join(value.strip().split())
    if not content or len(content) > max_chars:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return content


def sanitize_for_extraction(value: str) -> str:
    """Remove known credential-shaped fragments before the optional model call."""

    if not isinstance(value, str):
        return ""
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[敏感内容已移除]", sanitized)
    return sanitized


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_category(category: str) -> str:
    if category not in MEMORY_CATEGORIES:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return category


def validate_subject(subject_key: str | None) -> str | None:
    if subject_key is None:
        return None
    value = " ".join(subject_key.strip().split())
    if not value or len(value) > 300 or any(char in value for char in "\r\n"):
        raise APIError(ErrorCode.VALIDATION_ERROR)
    return value


async def locked_state(session: AsyncSession, user_id: int) -> MemoryState:
    await session.execute(
        insert(MemoryState)
        .values(user_id=user_id, memory_epoch=0)
        .on_conflict_do_nothing(index_elements=[MemoryState.user_id])
    )
    state = await session.scalar(
        select(MemoryState)
        .where(MemoryState.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None:
        raise APIError(ErrorCode.INTERNAL_ERROR)
    return state


def memory_output(item: Memory) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "category": item.category,
        "subject_key": item.subject_key,
        "content": item.content,
        "origin": item.origin,
        "source_message_id": str(item.source_message_id) if item.source_message_id else None,
        "revision": item.revision,
        "created_at": item.created_at.astimezone(UTC).isoformat() if item.created_at else None,
        "updated_at": item.updated_at.astimezone(UTC).isoformat() if item.updated_at else None,
    }


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    category: str | None = None,
    limit: int = 50,
    after_id: int = 0,
) -> list[Memory]:
    if category is not None:
        validate_category(category)
    query = select(Memory).where(Memory.user_id == user_id, Memory.id > after_id)
    if category is not None:
        query = query.where(Memory.category == category)
    return list((await session.scalars(query.order_by(Memory.id).limit(min(limit, 100) + 1))).all())


async def select_for_context(
    session: AsyncSession,
    user_id: int,
    question: str,
    *,
    limit: int = MAX_INJECT_ITEMS,
    max_candidates: int = MAX_SELECT_CANDIDATES,
    max_inject_chars: int = MAX_INJECT_CHARS,
) -> tuple[Memory, ...]:
    rows = list(
        (
            await session.scalars(
                select(Memory)
                .where(Memory.user_id == user_id)
                .order_by(Memory.updated_at.desc(), Memory.id.desc())
                .limit(min(max(1, max_candidates), MAX_SELECT_CANDIDATES))
            )
        ).all()
    )
    tokens = {token.casefold() for token in re.findall(r"[\w\u4e00-\u9fff]+", question)}

    def score(item: Memory) -> tuple[int, float, int]:
        subject = {
            token.casefold() for token in re.findall(r"[\w\u4e00-\u9fff]+", item.subject_key or "")
        }
        content = {token.casefold() for token in re.findall(r"[\w\u4e00-\u9fff]+", item.content)}
        related = len(tokens & (subject | content))
        general = 1 if item.category == "preference" else 0
        stamp = item.updated_at.timestamp() if item.updated_at else 0.0
        return related + general, stamp, -item.id

    selected: list[Memory] = []
    used = 0
    for item in sorted(rows, key=score, reverse=True):
        if len(selected) >= min(limit, MAX_INJECT_ITEMS):
            break
        if used + len(item.content) > max_inject_chars:
            continue
        selected.append(item)
        used += len(item.content)
    return tuple(selected)


async def save_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    content: str,
    category: str,
    subject_key: str | None = None,
    memory_id: int | None = None,
    expected_revision: int | None = None,
    origin: str = "manual",
    source_message_id: int | None = None,
    max_chars: int = MAX_MEMORY_CHARS,
) -> tuple[Memory, str, int]:
    if origin not in MEMORY_ORIGINS:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    value = normalized_content(content, max_chars=max_chars)
    category = validate_category(category)
    subject_key = validate_subject(subject_key)
    if memory_id is not None and memory_id < 1:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    if expected_revision is not None and expected_revision < 1:
        raise APIError(ErrorCode.VALIDATION_ERROR)

    state = await locked_state(session, user_id)
    query = select(Memory).where(Memory.user_id == user_id)
    if memory_id is not None:
        query = query.where(Memory.id == memory_id)
    elif subject_key is not None:
        query = query.where(Memory.subject_key == subject_key)
    else:
        query = query.where(Memory.content_hash == content_hash(value), Memory.category == category)
    item = await session.scalar(query.with_for_update())
    if memory_id is not None and item is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if item is not None and expected_revision is not None and item.revision != expected_revision:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    digest = content_hash(value)
    if (
        item is not None
        and item.content == value
        and item.category == category
        and item.subject_key == subject_key
    ):
        return item, "unchanged", state.memory_epoch
    now = datetime.now(UTC)
    if item is None:
        item = Memory(
            user_id=user_id,
            category=category,
            subject_key=subject_key,
            content=value,
            content_hash=digest,
            origin=origin,
            source_message_id=source_message_id,
            revision=1,
        )
        session.add(item)
        result = "created"
    else:
        item.category = category
        item.subject_key = subject_key
        item.content = value
        item.content_hash = digest
        item.origin = origin
        item.source_message_id = source_message_id
        item.revision += 1
        item.updated_at = now
        result = "updated"
    state.memory_epoch += 1
    state.updated_at = now
    await session.flush()
    return item, result, state.memory_epoch


async def delete_for_user(
    session: AsyncSession, user_id: int, memory_id: int, *, expected_revision: int | None = None
) -> int:
    if memory_id < 1:
        raise APIError(ErrorCode.VALIDATION_ERROR)
    state = await locked_state(session, user_id)
    item = await session.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update()
    )
    if item is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    if expected_revision is not None and item.revision != expected_revision:
        raise APIError(ErrorCode.ROW_VERSION_CONFLICT)
    await session.delete(item)
    state.memory_epoch += 1
    state.updated_at = datetime.now(UTC)
    await session.flush()
    return state.memory_epoch


async def current_epoch(session: AsyncSession, user_id: int) -> int:
    state = await session.scalar(select(MemoryState).where(MemoryState.user_id == user_id))
    return state.memory_epoch if state is not None else 0


async def apply_candidates(
    session: AsyncSession,
    user_id: int,
    captured_epoch: int,
    source_message_id: int,
    candidates: Iterable[dict[str, Any]],
    max_candidates: int = 3,
    max_chars: int = MAX_MEMORY_CHARS,
) -> dict[str, Any]:
    state = await locked_state(session, user_id)
    if state.memory_epoch != captured_epoch:
        return {"status": "stale", "saved": 0, "epoch": state.memory_epoch}
    saved = 0
    seen: set[tuple[str, str | None]] = set()
    for candidate in list(candidates)[: max(0, max_candidates)]:
        if not isinstance(candidate, dict):
            continue
        allowed = {"category", "subject_key", "content"}
        if set(candidate) - allowed:
            continue
        category = candidate.get("category")
        subject_key = candidate.get("subject_key")
        value = candidate.get("content")
        if not isinstance(category, str) or not isinstance(value, str):
            continue
        try:
            category = validate_category(category)
            subject_key = validate_subject(subject_key if isinstance(subject_key, str) else None)
            value = normalized_content(value, max_chars=max_chars)
        except APIError:
            continue
        marker = (category, subject_key)
        if marker in seen:
            continue
        seen.add(marker)
        lookup = (
            Memory.subject_key == subject_key
            if subject_key is not None
            else Memory.content_hash == content_hash(value)
        )
        item = await session.scalar(
            select(Memory).where(Memory.user_id == user_id, lookup).with_for_update()
        )
        if item is not None and item.content == value and item.category == category:
            continue
        if item is None:
            item = Memory(
                user_id=user_id,
                category=category,
                subject_key=subject_key,
                content=value,
                content_hash=content_hash(value),
                origin="automatic",
                source_message_id=source_message_id,
            )
            session.add(item)
        else:
            # Automatic extraction never overwrites an explicit/manual value
            # without a reliable explicit correction in the same request.
            if item.origin in {"explicit", "manual"}:
                continue
            item.category = category
            item.content = value
            item.content_hash = content_hash(value)
            item.origin = "automatic"
            item.source_message_id = source_message_id
            item.revision += 1
            item.updated_at = datetime.now(UTC)
        saved += 1
    if saved:
        state.memory_epoch += 1
        state.updated_at = datetime.now(UTC)
    await session.flush()
    return {"status": "applied", "saved": saved, "epoch": state.memory_epoch}


__all__ = [
    "MAX_INJECT_CHARS",
    "MAX_INJECT_ITEMS",
    "MAX_MEMORY_CHARS",
    "apply_candidates",
    "content_hash",
    "current_epoch",
    "delete_for_user",
    "list_for_user",
    "locked_state",
    "memory_output",
    "normalized_content",
    "sanitize_for_extraction",
    "save_for_user",
    "select_for_context",
    "validate_category",
]
