"""Short-lived, process-local database query definitions for safe continuation."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from threading import Lock
from uuid import uuid4

from enterprise_mind.database.contracts import GuardedQuery
from enterprise_mind.database.query import DatabaseAnswer, PreparedQuery

QUERY_CONTEXT_TTL_SECONDS = 30 * 60
MAX_QUERY_CONTEXTS = 256


@dataclass(frozen=True, repr=False)
class DatabaseQueryContext:
    """A guarded query definition that can be used for one explicit next page."""

    query_id: str
    owner_user_id: int
    conversation_id: int
    prepared: PreparedQuery
    guarded_query: GuardedQuery
    query_kind: str
    conditions_summary: str
    current_page_index: int
    has_more: bool
    continuation_available: bool
    current_result_id: str | None
    expires_at: float

    @property
    def next_page_index(self) -> int:
        return self.current_page_index + 1

    def __repr__(self) -> str:
        return (
            "DatabaseQueryContext(query_id=<redacted>, prepared=<redacted>, "
            "guarded_query=<redacted>)"
        )


class DatabaseQueryContextStore:
    """Bounded memory for the current query definition; never a durable store."""

    def __init__(self, *, ttl_seconds: int = QUERY_CONTEXT_TTL_SECONDS) -> None:
        if ttl_seconds < 1:
            raise ValueError("query context TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, DatabaseQueryContext] = {}
        self._latest: dict[tuple[int, int], str] = {}
        self._lock = Lock()

    def start(
        self, *, owner_user_id: int, conversation_id: int, answer: DatabaseAnswer
    ) -> DatabaseQueryContext:
        if answer.guarded_query is None:
            raise ValueError("database answer has no guarded query")
        context = DatabaseQueryContext(
            query_id=uuid4().hex,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            prepared=answer.prepared,
            guarded_query=answer.guarded_query,
            query_kind=answer.query_kind,
            conditions_summary=answer.conditions_summary,
            current_page_index=answer.prepared.page_index,
            has_more=answer.result.has_more,
            continuation_available=answer.continuation_available,
            current_result_id=None,
            expires_at=time.monotonic() + self.ttl_seconds,
        )
        with self._lock:
            self._put(context)
        return context

    def advance(
        self,
        context: DatabaseQueryContext,
        *,
        owner_user_id: int,
        conversation_id: int,
        answer: DatabaseAnswer,
    ) -> DatabaseQueryContext:
        if (
            context.owner_user_id != owner_user_id
            or context.conversation_id != conversation_id
            or answer.guarded_query is None
            or answer.prepared.page_index != context.next_page_index
        ):
            raise ValueError("database query context does not match next page")
        updated = replace(
            context,
            prepared=answer.prepared,
            guarded_query=answer.guarded_query,
            current_page_index=answer.prepared.page_index,
            has_more=answer.result.has_more,
            continuation_available=answer.continuation_available,
            current_result_id=None,
            expires_at=time.monotonic() + self.ttl_seconds,
        )
        with self._lock:
            self._put(updated)
        return updated

    def update_has_more(
        self,
        context: DatabaseQueryContext,
        *,
        owner_user_id: int,
        conversation_id: int,
        has_more: bool,
    ) -> DatabaseQueryContext:
        """Sync result-budget truncation without changing the query definition."""

        now = time.monotonic()
        with self._lock:
            self._purge(now)
            current = self._items.get(context.query_id)
            if (
                current is None
                or current.owner_user_id != owner_user_id
                or current.conversation_id != conversation_id
                or self._latest.get((owner_user_id, conversation_id)) != context.query_id
            ):
                raise ValueError("database query context is no longer current")
            updated = replace(current, has_more=bool(has_more))
            self._items[context.query_id] = updated
            return updated

    def bind_result(
        self,
        context: DatabaseQueryContext,
        *,
        owner_user_id: int,
        conversation_id: int,
        result_id: str,
    ) -> DatabaseQueryContext:
        """Bind the only currently valid continuation token to a result page."""

        if not result_id:
            raise ValueError("result_id is required")
        with self._lock:
            self._purge(time.monotonic())
            current = self._items.get(context.query_id)
            if (
                current is None
                or current.owner_user_id != owner_user_id
                or current.conversation_id != conversation_id
                or self._latest.get((owner_user_id, conversation_id)) != context.query_id
            ):
                raise ValueError("database query context is no longer current")
            updated = replace(
                current,
                current_result_id=result_id,
                expires_at=time.monotonic() + self.ttl_seconds,
            )
            self._items[context.query_id] = updated
            return updated

    def invalidate(
        self,
        query_id: str | None,
        *,
        owner_user_id: int,
        conversation_id: int,
    ) -> None:
        """Remove a query definition after an explicit non-recoverable failure."""

        if not query_id:
            return
        with self._lock:
            context = self._items.get(query_id)
            if context is None or (
                context.owner_user_id != owner_user_id or context.conversation_id != conversation_id
            ):
                return
            self._items.pop(query_id, None)
            if self._latest.get((owner_user_id, conversation_id)) == query_id:
                self._latest.pop((owner_user_id, conversation_id), None)

    def latest(self, *, owner_user_id: int, conversation_id: int) -> DatabaseQueryContext | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            query_id = self._latest.get((owner_user_id, conversation_id))
            return self._items.get(query_id) if query_id else None

    def get(
        self, query_id: str | None, *, owner_user_id: int, conversation_id: int
    ) -> DatabaseQueryContext | None:
        if not query_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            context = self._items.get(query_id)
            if context is None or (
                context.owner_user_id != owner_user_id or context.conversation_id != conversation_id
            ):
                return None
            return context

    def _put(self, context: DatabaseQueryContext) -> None:
        self._purge(time.monotonic())
        key = (context.owner_user_id, context.conversation_id)
        previous = self._latest.get(key)
        if previous and previous != context.query_id:
            self._items.pop(previous, None)
        self._items[context.query_id] = context
        self._latest[key] = context.query_id
        while len(self._items) > MAX_QUERY_CONTEXTS:
            oldest = next(iter(self._items))
            removed = self._items.pop(oldest)
            key = (removed.owner_user_id, removed.conversation_id)
            if self._latest.get(key) == oldest:
                self._latest.pop(key, None)

    def _purge(self, now: float) -> None:
        for query_id, context in list(self._items.items()):
            if context.expires_at <= now:
                self._items.pop(query_id, None)
                key = (context.owner_user_id, context.conversation_id)
                if self._latest.get(key) == query_id:
                    self._latest.pop(key, None)


__all__ = [
    "DatabaseQueryContext",
    "DatabaseQueryContextStore",
    "MAX_QUERY_CONTEXTS",
    "QUERY_CONTEXT_TTL_SECONDS",
]
