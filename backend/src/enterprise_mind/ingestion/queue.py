from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from enterprise_mind.infrastructure.settings import IngestionSettings, read_secret

GROUP = "ingestion-workers"


class TaskQueue:
    def __init__(self, settings: IngestionSettings) -> None:
        self.stream = settings.redis_stream
        self.redis: Redis = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            username=settings.redis_username,
            password=read_secret(settings.redis_password_file).get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    async def check(self) -> None:
        await cast(Awaitable[bool], self.redis.ping())

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    async def publish(self, event_id: int) -> None:
        await self.ensure_group()
        await self.redis.xadd(self.stream, {"event_id": str(event_id)})

    async def receive(self, consumer: str, lease_seconds: int) -> tuple[str, dict[str, Any]] | None:
        await self.ensure_group()
        claimed = await self.redis.xautoclaim(
            self.stream,
            GROUP,
            consumer,
            min_idle_time=lease_seconds * 1000,
            start_id="0-0",
            count=1,
        )
        if claimed[1]:
            entry = claimed[1][0]
            return str(entry[0]), dict(entry[1])
        messages = await self.redis.xreadgroup(
            GROUP, consumer, {self.stream: ">"}, count=1, block=1000
        )
        if not messages:
            return None
        entry = messages[0][1][0]
        return str(entry[0]), dict(entry[1])

    async def ack(self, message_id: str) -> None:
        # One consumer group; delete only after durable handling, never trim unacknowledged work.
        await self.redis.xack(self.stream, GROUP, message_id)
        await self.redis.xdel(self.stream, message_id)

    async def close(self) -> None:
        await self.redis.aclose()
