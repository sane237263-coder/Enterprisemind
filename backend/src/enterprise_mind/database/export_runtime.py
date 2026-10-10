"""Process-local plans for database export tasks.

The durable task stores only safe provenance and an opaque handle.  Guarded SQL,
parameters, and current-page rows stay in this bounded process-local store and
are discarded on expiry or terminal task completion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4

from enterprise_mind.database.contracts import GuardedQuery, SchemaCatalog
from enterprise_mind.database.pages import DatabasePage
from enterprise_mind.database.projection import BoundSchema

EXPORT_PLAN_TTL_SECONDS = 30 * 60
MAX_EXPORT_PLANS = 256


@dataclass(frozen=True, repr=False)
class ExportRuntimePlan:
    handle: str
    owner_user_id: int
    conversation_id: int
    scope: str
    binding: BoundSchema
    catalog: SchemaCatalog
    guarded_query: GuardedQuery
    page: DatabasePage | None
    source_refs: tuple[dict[str, Any], ...]
    expires_at: float
    row_limit: int | None = None

    def __repr__(self) -> str:
        return (
            "ExportRuntimePlan(handle=<redacted>, owner_user_id="
            f"{self.owner_user_id!r}, scope={self.scope!r}, query=<redacted>, page=<redacted>)"
        )


class ExportRuntimePlanStore:
    """Bounded, non-persistent export input store."""

    def __init__(self, *, ttl_seconds: int = EXPORT_PLAN_TTL_SECONDS) -> None:
        if ttl_seconds < 1:
            raise ValueError("export plan TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, ExportRuntimePlan] = {}
        self._lock = Lock()

    def create_handle(self) -> str:
        return uuid4().hex

    def put(self, plan: ExportRuntimePlan) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            self._items[plan.handle] = plan
            while len(self._items) > MAX_EXPORT_PLANS:
                self._items.pop(next(iter(self._items)))

    def get(
        self, handle: str | None, *, owner_user_id: int, conversation_id: int
    ) -> ExportRuntimePlan | None:
        if not handle:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            plan = self._items.get(handle)
            if plan is None or plan.owner_user_id != owner_user_id:
                return None
            if plan.conversation_id != conversation_id:
                return None
            return plan

    def remove(self, handle: str | None) -> None:
        if not handle:
            return
        with self._lock:
            self._items.pop(handle, None)

    def _purge(self, now: float) -> None:
        for handle, plan in list(self._items.items()):
            if plan.expires_at <= now:
                self._items.pop(handle, None)


__all__ = [
    "EXPORT_PLAN_TTL_SECONDS",
    "ExportRuntimePlan",
    "ExportRuntimePlanStore",
    "MAX_EXPORT_PLANS",
]
