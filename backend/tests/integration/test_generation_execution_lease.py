"""PostgreSQL and MinIO contracts for generation execution leases.

The remote cleanup retry case injects renderer bytes and a plan owner to test
publication ownership and retry behavior; it does not validate XLSX format or
claim a real Database MCP export.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.generation import service as generation_service
from enterprise_mind.generation import worker as generation_worker_module
from enterprise_mind.generation.rendering import RenderedFile
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.infrastructure.settings import WorkerConfig
from enterprise_mind.ingestion.models import OutboxEvent, StorageObject

pytestmark = pytest.mark.integration


def _create_render_task(runtime: Any) -> tuple[int, int]:
    key = "generation-lease-" + uuid.uuid4().hex

    async def create() -> tuple[int, int]:
        async with runtime.worker.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(User, Department)
                    .outerjoin(Department, Department.id == User.department_id)
                    .where(User.username == "初始管理员")
                )
            ).one_or_none()
            assert row is not None
            user, department = row
            identity = CurrentIdentity(user, department, user.credential_version)
            draft = await generation_service.create_draft(
                session,
                identity,
                title="Lease lifecycle",
                body_markdown="# Frozen lease input\n\nRendered by the real worker.",
                tables=[],
                source_refs=[],
            )
            task = await generation_service.create_render_task(
                session,
                identity,
                draft_id=int(draft.id),
                expected_revision=int(draft.revision),
                file_format="md",
                file_name=key + ".md",
                idempotency_key=key,
            )
            event = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_type == "generation_task",
                    OutboxEvent.aggregate_id == task.id,
                    OutboxEvent.event_type == "generation.requested",
                )
            )
            assert event is not None
            return int(task.id), int(event.id)

    return runtime.run(create)


def _set_claim(
    runtime: Any,
    task_id: int,
    *,
    owner: str,
    expires_at: datetime,
    attempts: int,
    handle: str | None = None,
) -> None:
    async def update() -> None:
        async with runtime.worker.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            assert task is not None
            task.status = "running"
            task.phase = "rendering"
            task.execution_owner = owner
            task.execution_lease_expires_at = expires_at
            task.execution_attempt_count = attempts
            if handle is not None:
                task.input_ref = {**dict(task.input_ref or {}), "runtime_handle": handle}

    runtime.run(update)


def _set_runtime_handle(runtime: Any, task_id: int, handle: str) -> None:
    async def update() -> None:
        async with runtime.worker.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            assert task is not None and task.status == "queued"
            task.input_ref = {**dict(task.input_ref or {}), "runtime_handle": handle}

    runtime.run(update)


def _expire_claim(runtime: Any, task_id: int) -> None:
    async def update() -> None:
        async with runtime.worker.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            assert task is not None and task.status == "running"
            task.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    runtime.run(update)


def _snapshot(database: Any, task_id: int) -> tuple[GenerationTask, int]:
    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        file_count = len(
            list(session.scalars(select(GeneratedFile).where(GeneratedFile.task_id == task_id)))
        )
        return task, file_count


def _published_generation_event(database: Any, event_id: int, *, old: bool = True) -> None:
    with Session(database.engine) as session, session.begin():
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        event.status = "published"
        event.published_at = datetime.now(UTC) - timedelta(seconds=121 if old else 0)


@pytest.mark.parametrize("state", ["queued", "expired", "exhausted"])
def test_lost_generation_stream_recovers_without_resetting_execution_budget(
    runtime, database, ingestion_settings, state: str
) -> None:
    task_id, event_id = _create_render_task(runtime)
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    _, _, redis = ingestion_settings
    assert redis.delete(runtime.worker.queue.stream) == 1
    _published_generation_event(database, event_id)
    attempts = 0
    if state != "queued":
        attempts = (
            runtime.worker.generation.config.generation_recovery_max_attempts
            if state == "exhausted"
            else 1
        )
        _set_claim(
            runtime,
            task_id,
            owner="a" * 64,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            attempts=attempts,
        )
    assert runtime.run(runtime.dispatcher.dispatch_once) == 1
    untouched, count = _snapshot(database, task_id)
    assert untouched.execution_attempt_count == attempts and count == 0
    assert runtime.run(runtime.worker.run_once)
    task, count = _snapshot(database, task_id)
    assert task.status == ("failed" if state == "exhausted" else "succeeded")
    assert count == (0 if state == "exhausted" else 1)
    assert task.execution_attempt_count == (attempts if state == "exhausted" else attempts + 1)
    assert task.execution_lease_expires_at is None
    assert redis.xlen(runtime.worker.queue.stream) == 0
    with Session(database.engine) as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None and event.attempt_count == 2
    assert runtime.run(runtime.dispatcher.recover) == 0


def test_generation_recovery_filters_before_batch_limit_and_preserves_live_owner(
    runtime, database
) -> None:
    ignored = []
    for state in ("succeeded", "failed", "cancelled", "live", "recent", "pending"):
        task_id, event_id = _create_render_task(runtime)
        _published_generation_event(database, event_id, old=state != "recent")
        with Session(database.engine) as session, session.begin():
            task = session.get(GenerationTask, task_id)
            event = session.get(OutboxEvent, event_id)
            assert task is not None and event is not None
            if state in {"succeeded", "failed", "cancelled"}:
                task.status = state
            elif state == "live":
                task.status = "running"
                task.execution_owner = "b" * 64
                task.execution_attempt_count = 1
                task.execution_lease_expires_at = datetime.now(UTC) + timedelta(seconds=300)
            elif state == "pending":
                event.status, event.published_at = "pending", None
            ignored.append((task_id, event_id, task.status, event.status, task.execution_owner))
    eligible_task, eligible_event = _create_render_task(runtime)
    _published_generation_event(database, eligible_event)
    runtime.dispatcher.config.outbox.claim_batch_size = 1
    assert runtime.run(runtime.dispatcher.recover) == 1
    with Session(database.engine) as session:
        event = session.get(OutboxEvent, eligible_event)
        task = session.get(GenerationTask, eligible_task)
        assert event is not None and event.status == "pending" and event.published_at is None
        assert task is not None and task.status == "queued" and task.execution_attempt_count == 0
        for task_id, event_id, task_status, event_status, owner in ignored:
            task = session.get(GenerationTask, task_id)
            event = session.get(OutboxEvent, event_id)
            assert task is not None and event is not None
            assert (task.status, event.status, task.execution_owner) == (
                task_status,
                event_status,
                owner,
            )
    assert runtime.run(runtime.dispatcher.recover) == 0


def test_live_lease_renews_and_terminal_replay_publishes_once(
    runtime, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, event_id = _create_render_task(runtime)
    worker = runtime.worker.generation
    worker.worker_config = WorkerConfig(lease_seconds=10, lease_renew_interval_seconds=1)
    rendered = threading.Event()
    release = threading.Event()
    real_render = generation_worker_module.render_to_path

    def render_then_wait(*args: Any, **kwargs: Any) -> Any:
        result = real_render(*args, **kwargs)
        rendered.set()
        if not release.wait(timeout=20):
            raise TimeoutError("the lease test did not release its renderer")
        return result

    monkeypatch.setattr(generation_worker_module, "render_to_path", render_then_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.run, worker.handle_event, event_id)
        try:
            assert rendered.wait(timeout=20)
            claimed, _ = _snapshot(database, task_id)
            first_owner = claimed.execution_owner
            initial_expiry = claimed.execution_lease_expires_at
            assert claimed.status == "running"
            assert claimed.execution_attempt_count == 1
            assert first_owner and initial_expiry is not None

            renewed_expiry = initial_expiry
            deadline = time.monotonic() + 5
            while renewed_expiry <= initial_expiry and time.monotonic() < deadline:
                time.sleep(0.05)
                claimed, _ = _snapshot(database, task_id)
                renewed_expiry = claimed.execution_lease_expires_at
            assert renewed_expiry > initial_expiry

            assert runtime.run(worker.handle_event, event_id) is False
            still_claimed, file_count = _snapshot(database, task_id)
            assert still_claimed.execution_owner == first_owner
            assert still_claimed.execution_attempt_count == 1
            assert file_count == 0
        finally:
            release.set()
        assert future.result(timeout=30) is True

    # A duplicate terminal delivery only ACKs the event; it does not render a
    # second file or increment the durable attempt count.
    assert runtime.run(worker.handle_event, event_id) is True
    task, file_count = _snapshot(database, task_id)
    assert task.status == "succeeded"
    assert task.execution_attempt_count == 1
    assert task.execution_lease_expires_at is None
    assert file_count == 1


def test_expired_attempt_is_reclaimed_and_old_owner_cannot_publish_or_cleanup(
    runtime, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, event_id = _create_render_task(runtime)
    worker = runtime.worker.generation
    plan_handle = "shared-plan-" + uuid.uuid4().hex
    _set_runtime_handle(runtime, task_id, plan_handle)

    class Plans:
        def __init__(self) -> None:
            self.handles = {plan_handle}

        def remove(self, handle: str | None) -> None:
            if handle is not None:
                self.handles.discard(handle)

    plans = Plans()
    worker.export_plans = plans
    real_render = generation_worker_module.render_to_path
    reached = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = 0
    calls_lock = threading.Lock()

    def render_with_barriers(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = real_render(*args, **kwargs)
        with calls_lock:
            index = calls
            calls += 1
        assert index < 2
        reached[index].set()
        if not release[index].wait(timeout=20):
            raise TimeoutError("the takeover test did not release its renderer")
        return result

    monkeypatch.setattr(generation_worker_module, "render_to_path", render_with_barriers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(runtime.run, worker.handle_event, event_id)
        try:
            assert reached[0].wait(timeout=20)
            running, _ = _snapshot(database, task_id)
            old_owner = running.execution_owner
            assert old_owner is not None
            assert running.execution_attempt_count == 1
            _expire_claim(runtime, task_id)

            new_future = pool.submit(runtime.run, worker.handle_event, event_id)
            assert reached[1].wait(timeout=20)
            current, _ = _snapshot(database, task_id)
            assert current.status == "running"
            assert current.execution_owner != old_owner
            new_owner = current.execution_owner
            assert new_owner is not None
            assert current.execution_attempt_count == 2

            # A stale attempt cannot fail the new owner or release its shared
            # export plan while that owner is still running.
            assert not runtime.run(
                worker._set_failed,
                task_id,
                old_owner,
                "GENERATION_FAILED",
                "stale attempt",
            )
            assert runtime.run(worker._cleanup_export_plan, task_id, plan_handle, old_owner) is None
            assert plans.handles == {plan_handle}

            release[0].set()
            assert old_future.result(timeout=20) is False
            current, file_count = _snapshot(database, task_id)
            assert current.status == "running"
            assert current.execution_owner == new_owner
            assert file_count == 0

            release[1].set()
            assert new_future.result(timeout=30) is True
        finally:
            release[0].set()
            release[1].set()

    current, file_count = _snapshot(database, task_id)
    assert current.status == "succeeded"
    assert current.execution_owner != old_owner
    assert current.execution_lease_expires_at is None
    assert current.execution_attempt_count == 2
    assert file_count == 1

    # A stale token still cannot clear the terminal attempt's plan. The
    # terminal cleanup retry uses the owner recorded by the terminal row.
    runtime.run(worker._cleanup_export_plan, task_id, plan_handle, old_owner)
    assert plans.handles == {plan_handle}
    assert runtime.run(worker._cleanup_terminal_export_plan, task_id, plan_handle)
    assert plans.handles == set()


def test_expired_attempt_stops_at_recovery_limit(runtime, database) -> None:
    task_id, event_id = _create_render_task(runtime)
    worker = runtime.worker.generation
    owner = "last-attempt-" + uuid.uuid4().hex
    maximum = worker.config.generation_recovery_max_attempts
    _set_claim(
        runtime,
        task_id,
        owner=owner,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        attempts=maximum,
    )

    assert runtime.run(worker.handle_event, event_id) is True
    task, file_count = _snapshot(database, task_id)
    assert task.status == "failed"
    assert task.error_code == "FILE_GENERATION_TIMEOUT"
    assert task.execution_attempt_count == maximum
    assert task.execution_owner == owner
    assert task.execution_lease_expires_at is None
    assert file_count == 0

    assert runtime.run(worker.handle_event, event_id) is True
    replayed, replayed_file_count = _snapshot(database, task_id)
    assert replayed.execution_attempt_count == maximum
    assert replayed_file_count == 0


def test_async_worker_cancellation_expires_attempt_and_allows_retry(
    runtime, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, event_id = _create_render_task(runtime)
    worker = runtime.worker.generation
    rendered = threading.Event()
    cancel_started = threading.Event()
    release = threading.Event()
    real_render = generation_worker_module.render_to_path

    def pause_first_render(*args: Any, **kwargs: Any) -> Any:
        result = real_render(*args, **kwargs)
        if not rendered.is_set():
            rendered.set()
            if not release.wait(timeout=20):
                raise TimeoutError("the cancellation test did not release its renderer")
        return result

    monkeypatch.setattr(generation_worker_module, "render_to_path", pause_first_render)

    async def cancel_after_render_starts() -> str:
        handler = asyncio.create_task(worker.handle_event(event_id))
        await asyncio.to_thread(rendered.wait, 20)
        cancel_started.set()
        handler.cancel()
        try:
            await handler
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.run, cancel_after_render_starts)
        try:
            assert rendered.wait(timeout=20)
            assert cancel_started.wait(timeout=20)
            during_drain, _ = _snapshot(database, task_id)
            assert during_drain.status == "running"
            assert during_drain.execution_lease_expires_at is not None
            assert during_drain.execution_lease_expires_at > datetime.now(UTC)
        finally:
            release.set()
        assert future.result(timeout=30) == "cancelled"

    cancelled_attempt, file_count = _snapshot(database, task_id)
    assert cancelled_attempt.status == "running"
    assert cancelled_attempt.execution_owner is not None
    assert cancelled_attempt.execution_lease_expires_at is not None
    assert cancelled_attempt.execution_lease_expires_at <= datetime.now(UTC)
    assert cancelled_attempt.execution_attempt_count == 1
    assert file_count == 0

    assert runtime.run(worker.handle_event, event_id) is True
    retried, file_count = _snapshot(database, task_id)
    assert retried.status == "succeeded"
    assert retried.execution_attempt_count == 2
    assert retried.execution_lease_expires_at is None
    assert file_count == 1


def test_failed_remote_terminal_cleanup_keeps_published_object_and_retries(
    runtime, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, event_id = _create_render_task(runtime)
    worker = runtime.worker.generation
    plan_handle = "controlled-remote-plan-" + uuid.uuid4().hex

    async def prepare_export_task() -> None:
        async with runtime.worker.sessions() as session, session.begin():
            task = await session.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            assert task is not None
            task.kind = "database_export"
            task.format = "xlsx"
            task.input_ref = {**dict(task.input_ref or {}), "runtime_handle": plan_handle}

    runtime.run(prepare_export_task)

    class RemotePlans:
        def __init__(self) -> None:
            self.calls = 0

        async def release_export_plan(
            self, _handle: str, *, owner_user_id: int, conversation_id: int
        ) -> None:
            assert owner_user_id > 0
            assert conversation_id == 0
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("controlled remote release failure")

    class LocalPlans:
        def __init__(self) -> None:
            self.handles = {plan_handle}

        def remove(self, handle: str | None) -> None:
            if handle is not None:
                self.handles.discard(handle)

    remote = RemotePlans()
    local = LocalPlans()
    worker.database = remote  # type: ignore[assignment]
    worker.export_plans = local  # type: ignore[assignment]

    async def controlled_render(
        _task: GenerationTask,
        _frozen: dict[str, Any],
        directory: Path,
        *,
        execution_owner: str,
    ) -> RenderedFile:
        assert execution_owner
        payload = b"controlled export bytes"
        path = directory / "controlled.xlsx"
        path.write_bytes(payload)
        return RenderedFile(
            path=path,
            file_format="xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=len(payload),
            content_sha256=hashlib.sha256(payload).hexdigest(),
            exported_rows=0,
        )

    monkeypatch.setattr(worker, "_render", controlled_render)
    assert runtime.run(worker.handle_event, event_id) is False

    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None and task.status == "succeeded"
        generated = session.scalars(
            select(GeneratedFile).where(GeneratedFile.task_id == task_id)
        ).one()
        stored = session.get(StorageObject, generated.storage_object_id)
        assert stored is not None
        published_key = stored.object_key
    assert remote.calls == 1
    assert local.handles == {plan_handle}
    digest = hashlib.sha256(b"controlled export bytes").hexdigest()
    runtime.worker.storage.verify(published_key, len(b"controlled export bytes"), digest)

    # A terminal replay retries the failed shared-plan cleanup without
    # removing the already published export object.
    assert runtime.run(worker.handle_event, event_id) is True
    assert remote.calls == 2
    assert local.handles == set()
    runtime.worker.storage.verify(published_key, len(b"controlled export bytes"), digest)
