import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import String, cast, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_mind.agent.generation_models import GenerationTask
from enterprise_mind.infrastructure.settings import ApplicationConfig
from enterprise_mind.ingestion.models import (
    OutboxEvent,
    ProcessingTask,
    ProcessingUnit,
    StorageObject,
)
from enterprise_mind.ingestion.queue import TaskQueue
from enterprise_mind.ingestion.service import processing_event
from enterprise_mind.ingestion.storage import ObjectStorage
from enterprise_mind.ingestion.worker import RECOVERABLE_STAGES, lock_task

_GENERATION_ORPHAN_KEY = re.compile(
    r"(?P<prefix>generated|export)/"
    r"(?P<created>\d{8}T\d{6}Z)/"
    r"task-(?P<task_id>[1-9][0-9]{0,18})/"
    r"attempt-(?P<owner>[0-9a-f]{64})/"
    r"(?P<object_id>[0-9a-f]{32})\Z"
)
_MAX_SIGNED_BIGINT = (1 << 63) - 1


def _older_than_grace(value: datetime | None, threshold: datetime) -> bool:
    return (
        value is not None
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value < threshold
    )


def _list_objects_in_prefix(storage: ObjectStorage, prefix: str) -> list[Any]:
    return list(storage.client.list_objects(storage.bucket, prefix=prefix, recursive=True))


class Dispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        queue: TaskQueue,
        config: ApplicationConfig,
    ) -> None:
        self.sessions, self.queue, self.config = sessions, queue, config

    async def recover(self) -> int:
        now = datetime.now(UTC)
        redeliver_before = now - timedelta(seconds=self.config.outbox.redelivery_seconds)
        # Filter before LIMIT so recently published or already queued tasks cannot starve recovery.
        async with self.sessions() as session, session.begin():
            task_ids = list(
                (
                    await session.scalars(
                        select(ProcessingUnit.latest_task_id)
                        .outerjoin(
                            OutboxEvent,
                            OutboxEvent.deduplication_key
                            == "document.processing:" + cast(ProcessingUnit.latest_task_id, String),
                        )
                        .where(
                            ProcessingUnit.stage.in_(RECOVERABLE_STAGES),
                            or_(
                                (ProcessingUnit.status == "pending")
                                & or_(
                                    OutboxEvent.id.is_(None),
                                    (OutboxEvent.status == "published")
                                    & (OutboxEvent.published_at <= redeliver_before),
                                ),
                                (ProcessingUnit.status == "processing")
                                & (ProcessingUnit.lease_expires_at <= now),
                            ),
                        )
                        .distinct()
                        .order_by(ProcessingUnit.latest_task_id)
                        .limit(self.config.outbox.claim_batch_size)
                    )
                ).all()
            )
        recovered = 0
        for task_id in task_ids:
            async with self.sessions() as session, session.begin():
                state = await lock_task(session, task_id)
                if state is None or state[2].status not in {"pending", "processing"}:
                    continue
                document, revision, task = state
                units = list(
                    (
                        await session.scalars(
                            select(ProcessingUnit)
                            .where(
                                ProcessingUnit.latest_task_id == task_id,
                                ProcessingUnit.stage.in_(RECOVERABLE_STAGES),
                            )
                            .order_by(ProcessingUnit.id)
                            .with_for_update()
                        )
                    ).all()
                )
                for unit in units:
                    if (
                        unit.status == "processing"
                        and unit.lease_expires_at is not None
                        and unit.lease_expires_at <= now
                    ):
                        unit.status = "pending"
                        unit.lease_owner = unit.lease_expires_at = None
                        unit.updated_at = now
                        recovered += 1
                if not any(unit.status == "pending" for unit in units):
                    continue
                event = await session.scalar(
                    select(OutboxEvent)
                    .where(OutboxEvent.deduplication_key == f"document.processing:{task_id}")
                    .with_for_update()
                )
                if event is None:
                    session.add(processing_event(document.id, revision.id, task.id))
                elif (
                    event.status == "published"
                    and event.published_at is not None
                    and event.published_at
                    <= now - timedelta(seconds=self.config.outbox.redelivery_seconds)
                ):
                    event.status, event.next_attempt_at = "pending", now
                    event.published_at = None
        return recovered + await self._recover_generation(now, redeliver_before)

    async def _recover_generation(self, now: datetime, redeliver_before: datetime) -> int:
        # Redis may lose a published entry. Durable tasks remain recoverable,
        # while terminal tasks and live execution leases must not be redelivered.
        eligible = or_(
            GenerationTask.status == "queued",
            (GenerationTask.status == "running")
            & or_(
                GenerationTask.execution_lease_expires_at.is_(None),
                GenerationTask.execution_lease_expires_at <= now,
            ),
        )
        async with self.sessions() as session, session.begin():
            candidates = list(
                (
                    await session.execute(
                        select(GenerationTask.id, OutboxEvent.id)
                        .join(
                            OutboxEvent,
                            (OutboxEvent.aggregate_type == "generation_task")
                            & (OutboxEvent.aggregate_id == GenerationTask.id),
                        )
                        .where(
                            eligible,
                            OutboxEvent.event_type.in_(
                                ("generation.requested", "generation.export.requested")
                            ),
                            OutboxEvent.status == "published",
                            OutboxEvent.published_at <= redeliver_before,
                        )
                        .order_by(GenerationTask.id, OutboxEvent.id)
                        .limit(self.config.outbox.claim_batch_size)
                    )
                ).all()
            )
        recovered = 0
        for task_id, event_id in candidates:
            async with self.sessions() as session, session.begin():
                # Match generation's task-first lock order and recheck state
                # after locking, so completion/renewal wins over stale scans.
                task = await session.scalar(
                    select(GenerationTask)
                    .where(GenerationTask.id == task_id, eligible)
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                event = await session.scalar(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
                if (
                    event is None
                    or event.status != "published"
                    or event.published_at is None
                    or event.published_at > redeliver_before
                ):
                    continue
                event.status, event.next_attempt_at = "pending", now
                event.published_at = None
                recovered += 1
        return recovered

    async def dispatch_once(self) -> int:
        from enterprise_mind.ingestion.cleanup import CLEANUP_EVENTS, schedule_cleanup

        await schedule_cleanup(
            self.sessions,
            self.config.outbox.redelivery_seconds,
            self.config.outbox.claim_batch_size,
        )
        await self.recover()
        owner = uuid4().hex
        now = datetime.now(UTC)
        async with self.sessions() as session, session.begin():
            events = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.event_type.in_(
                                (
                                    "document.processing.requested",
                                    "generation.requested",
                                    "generation.export.requested",
                                    *CLEANUP_EVENTS,
                                )
                            ),
                            or_(
                                (OutboxEvent.status.in_(["pending", "failed"]))
                                & (OutboxEvent.next_attempt_at <= now),
                                (OutboxEvent.status == "publishing")
                                & (OutboxEvent.lease_expires_at <= now),
                            ),
                        )
                        .order_by(OutboxEvent.id)
                        .with_for_update(skip_locked=True)
                        .limit(self.config.outbox.claim_batch_size)
                    )
                ).all()
            )
            claimed = []
            for claimed_event in events:
                claimed_event.status, claimed_event.lease_owner = "publishing", owner
                claimed_event.lease_expires_at = now + timedelta(
                    seconds=self.config.worker.lease_seconds
                )
                claimed_event.attempt_count += 1
                claimed.append(claimed_event.id)
        published = 0
        for event_id in claimed:
            error = False
            try:
                await self.queue.publish(event_id)
            except Exception:
                error = True
            async with self.sessions() as session, session.begin():
                event = await session.scalar(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
                if (
                    event is None
                    or event.status != "publishing"
                    or event.lease_owner != owner
                    or event.lease_expires_at is None
                    or event.lease_expires_at <= datetime.now(UTC)
                ):
                    continue
                event.lease_owner = event.lease_expires_at = None
                if error:
                    event.status, event.last_error = "failed", "任务队列暂时不可用。"
                    event.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(60, 2 ** min(event.attempt_count, 6))
                    )
                else:
                    event.status, event.last_error = "published", None
                    event.published_at = datetime.now(UTC)
                    published += 1
        await self.expire_published()
        return published

    async def expire_published(self) -> None:
        threshold = datetime.now(UTC) - timedelta(days=self.config.outbox.published_retention_days)
        async with self.sessions() as session, session.begin():
            # Active processing events are recoverable until supported units finish.
            ids = list(
                (
                    await session.scalars(
                        select(OutboxEvent.id)
                        .where(
                            OutboxEvent.event_type == "document.processing.requested",
                            OutboxEvent.status == "published",
                            OutboxEvent.published_at < threshold,
                            ~select(ProcessingUnit.id)
                            .join(
                                ProcessingTask, ProcessingTask.id == ProcessingUnit.latest_task_id
                            )
                            .where(
                                ProcessingTask.revision_id == OutboxEvent.aggregate_id,
                                ProcessingUnit.stage.in_(RECOVERABLE_STAGES),
                                ProcessingUnit.status.in_(["pending", "processing"]),
                            )
                            .exists(),
                        )
                        .limit(self.config.outbox.claim_batch_size)
                    )
                ).all()
            )
            if ids:
                await session.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(ids)))


async def sweep_orphans(
    sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage, grace_seconds: int
) -> int:
    threshold = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    candidates = await asyncio.to_thread(
        lambda: list(
            storage.client.list_objects(storage.bucket, prefix="original/", recursive=True)
        )
    )
    removed = 0
    for candidate in candidates:
        key = candidate.object_name
        if not key or not re.fullmatch(r"original/\d{8}T\d{6}Z/[0-9a-f]{32}", key):
            continue
        if candidate.last_modified is None or candidate.last_modified >= threshold:
            continue
        try:
            created = datetime.strptime(key.split("/")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if created >= threshold:
            continue
        async with sessions() as session, session.begin():
            registered = await session.scalar(
                select(StorageObject.id)
                .where(StorageObject.bucket_name == storage.bucket, StorageObject.object_key == key)
                .limit(1)
            )
        # Upload deadlines are shorter than the grace window.
        if registered is None:
            await asyncio.to_thread(storage.remove, key)
            removed += 1

    # Generation uploads use a task and lease identity in their keys.  The row
    # lock serializes collection with publication, and skip_locked leaves a
    # publisher in flight alone.  Keep missing tasks and nonconforming keys for
    # manual review because the key itself is not enough to prove ownership.
    for prefix in ("generated", "export"):
        candidates = await asyncio.to_thread(_list_objects_in_prefix, storage, prefix + "/")
        for candidate in candidates:
            key = candidate.object_name
            match = _GENERATION_ORPHAN_KEY.fullmatch(key or "")
            if match is None or match.group("prefix") != prefix:
                continue
            try:
                created = datetime.strptime(match.group("created"), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                )
                task_id = int(match.group("task_id"))
            except (OverflowError, ValueError):
                continue
            if (
                task_id > _MAX_SIGNED_BIGINT
                or created >= threshold
                or not _older_than_grace(candidate.last_modified, threshold)
            ):
                continue

            owner_token = match.group("owner")
            async with sessions() as session, session.begin():
                task = await session.scalar(
                    select(GenerationTask)
                    .where(GenerationTask.id == task_id)
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                if task.status not in {"running", "succeeded", "failed", "cancelled"}:
                    continue
                expected_prefix = (
                    "generated"
                    if task.kind == "content_render"
                    else "export"
                    if task.kind in {"database_export", "chart_render"}
                    else None
                )
                if expected_prefix != prefix:
                    continue
                registered = await session.scalar(
                    select(StorageObject.id)
                    .where(
                        StorageObject.bucket_name == storage.bucket,
                        StorageObject.object_key == key,
                    )
                    .limit(1)
                )
                if registered is not None:
                    continue

                now = datetime.now(UTC)
                if task.status == "running" and task.execution_owner == owner_token:
                    lease_expires_at = task.execution_lease_expires_at
                    if lease_expires_at is None or lease_expires_at > now:
                        continue
                    # Commit this fence before touching MinIO.  A worker holding
                    # the former token can no longer renew or publish this key.
                    task.execution_owner = None
                    task.execution_lease_expires_at = None
                    task.updated_at = now

            # A slow/late PUT may finish after the initial listing.  Recheck the
            # server's current mtime after releasing the task row lock; if a
            # fresh object appeared, a later sweep can collect it after grace.
            try:
                current = await asyncio.to_thread(storage.client.stat_object, storage.bucket, key)
            except Exception:
                continue
            if not _older_than_grace(current.last_modified, threshold):
                continue
            try:
                await asyncio.to_thread(storage.remove, key)
            except Exception:
                # Leave the object for the next hourly sweep to retry.
                continue
            removed += 1
    return removed
