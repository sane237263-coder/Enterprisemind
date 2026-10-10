"""Ephemeral answer deltas. PostgreSQL remains the source for reconnection snapshots."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

EVENTS = {
    "run.status",
    "step.updated",
    "answer.delta",
    "pending_action.required",
    "run.completed",
    "run.failed",
    "run.cancelled",
}


class EventBus:
    def __init__(self, redis: Redis | None = None, prefix: str = "enterprise-mind:agent") -> None:
        self.redis, self.prefix = redis, prefix
        self.local: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}

    async def publish(self, run_id: int, event: str, data: dict[str, Any]) -> None:
        if event not in EVENTS:
            raise ValueError("unknown agent event")
        item = {"event": event, "data": data}
        for queue in tuple(self.local.get(run_id, ())):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)
        if self.redis is not None:
            try:
                await self.redis.publish(
                    f"{self.prefix}:{run_id}", json.dumps(item, ensure_ascii=False)
                )
            except RedisError:
                # A missed increment does not invalidate the durable final message.
                pass

    @asynccontextmanager
    async def subscribe(self, run_id: int) -> AsyncIterator[Any]:
        if self.redis is not None:
            subscription = self.redis.pubsub()
            subscribed = False
            try:
                # Subscribe before the SSE route yields its initial snapshot;
                # otherwise early answer chunks can fall into that gap.
                try:
                    await subscription.subscribe(f"{self.prefix}:{run_id}")
                    subscribed = True
                except RedisError:
                    pass  # The reader retries; durable snapshots remain available.

                async def read(wait_seconds: float) -> dict[str, Any] | None:
                    nonlocal subscribed
                    try:
                        if not subscribed:
                            await subscription.subscribe(f"{self.prefix}:{run_id}")
                            subscribed = True
                        result = await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=wait_seconds
                        )
                    except RedisError:
                        subscribed = False
                        await asyncio.sleep(wait_seconds)
                        return None
                    if result and result["type"] == "message":
                        try:
                            data = json.loads(result["data"])
                        except (ValueError, TypeError):
                            return None
                        if (
                            isinstance(data, dict)
                            and data.get("event") in EVENTS
                            and isinstance(data.get("data"), dict)
                        ):
                            return data
                    return None

                yield read
            finally:
                with suppress(RedisError):
                    await subscription.aclose()  # type: ignore[no-untyped-call]
        else:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
            self.local.setdefault(run_id, set()).add(queue)

            async def local_read(wait_seconds: float) -> dict[str, Any] | None:
                try:
                    return await asyncio.wait_for(queue.get(), wait_seconds)
                except TimeoutError:
                    return None

            try:
                yield local_read
            finally:
                self.local[run_id].discard(queue)
                if not self.local[run_id]:
                    del self.local[run_id]
