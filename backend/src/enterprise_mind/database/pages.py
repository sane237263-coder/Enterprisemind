"""Short-lived, process-local database result pages for source previews.

The page cache intentionally does not persist SQL, bind values, prompts or
rows.  Historical AnswerSource rows keep only safe provenance metadata; once
this bounded cache expires, a source preview can show metadata but cannot
re-execute or reconstruct the old result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any
from uuid import uuid4

from enterprise_mind.database.query import DatabaseAnswer

PAGE_TTL_SECONDS = 30 * 60
MAX_CACHED_PAGES = 256


@dataclass(frozen=True, repr=False)
class DatabasePage:
    query_id: str
    result_id: str
    owner_user_id: int
    conversation_id: int
    source_name: str
    database_name: str
    schema_snapshot_id: int
    referenced_relations: tuple[str, ...]
    queried_at: datetime
    conditions_summary: str
    query_kind: str
    page_size: int
    page_index: int
    columns: tuple[dict[str, str], ...]
    rows: tuple[dict[str, Any], ...]
    returned_rows: int
    has_more: bool
    continuation_available: bool
    source_version: int
    permission_fingerprint: str
    expires_at: float

    def __repr__(self) -> str:
        return (
            "DatabasePage(query_id=<redacted>, result_id=<redacted>, "
            "columns=<redacted>, rows=<redacted>)"
        )


class DatabasePageStore:
    """A bounded local cache; no page survives process replacement."""

    def __init__(self, *, ttl_seconds: int = PAGE_TTL_SECONDS) -> None:
        if ttl_seconds < 1:
            raise ValueError("page TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, DatabasePage] = {}
        self._query_results: dict[str, str] = {}
        self._lock = Lock()

    def put(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        answer: DatabaseAnswer,
        query_id: str | None = None,
    ) -> DatabasePage:
        now = time.monotonic()
        page = DatabasePage(
            query_id=query_id or uuid4().hex,
            result_id=uuid4().hex,
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            source_name=answer.prepared.binding.source_name,
            database_name=answer.prepared.binding.database_name,
            schema_snapshot_id=answer.prepared.binding.schema_snapshot_id,
            referenced_relations=answer.result.referenced_relations,
            queried_at=answer.queried_at,
            conditions_summary=answer.conditions_summary,
            query_kind=answer.query_kind,
            page_size=answer.prepared.page_size,
            page_index=answer.prepared.page_index,
            columns=tuple(
                {"name": column.name, "data_type": column.data_type}
                for column in answer.result.columns
            ),
            rows=tuple(dict(row) for row in answer.result.rows),
            returned_rows=answer.result.row_count,
            has_more=answer.result.has_more,
            continuation_available=answer.continuation_available,
            source_version=answer.prepared.binding.source_version,
            permission_fingerprint=answer.prepared.binding.permission_fingerprint,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._purge(now)
            previous_result = self._query_results.get(page.query_id)
            if previous_result is not None:
                self._items.pop(previous_result, None)
            self._items[page.result_id] = page
            self._query_results[page.query_id] = page.result_id
            while len(self._items) > MAX_CACHED_PAGES:
                oldest_result = next(iter(self._items))
                oldest_page = self._items.pop(oldest_result)
                if self._query_results.get(oldest_page.query_id) == oldest_result:
                    self._query_results.pop(oldest_page.query_id, None)
        return page

    def get(
        self,
        result_id: str | None,
        *,
        owner_user_id: int,
        conversation_id: int | None = None,
    ) -> DatabasePage | None:
        if not result_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            page = self._items.get(result_id)
            if (
                page is None
                or page.owner_user_id != owner_user_id
                or (conversation_id is not None and page.conversation_id != conversation_id)
            ):
                return None
            return page

    def _purge(self, now: float) -> None:
        expired = [key for key, page in self._items.items() if page.expires_at <= now]
        for key in expired:
            page = self._items.pop(key, None)
            if page is not None and self._query_results.get(page.query_id) == key:
                self._query_results.pop(page.query_id, None)


__all__ = ["DatabasePage", "DatabasePageStore", "MAX_CACHED_PAGES", "PAGE_TTL_SECONDS"]
