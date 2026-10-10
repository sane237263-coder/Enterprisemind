"""PostgreSQL and MinIO contracts for generated-object orphan collection."""

from __future__ import annotations

import hashlib
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from minio.error import S3Error
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_ingestion import ingestion_runtime  # noqa: F401

from enterprise_mind.agent.generation_models import GeneratedFile, GenerationTask
from enterprise_mind.generation import service as generation_service
from enterprise_mind.identity.models import Department, User
from enterprise_mind.identity.service import CurrentIdentity
from enterprise_mind.ingestion import dispatch as dispatch_module
from enterprise_mind.ingestion.dispatch import sweep_orphans
from enterprise_mind.ingestion.models import OutboxEvent, StorageObject

pytestmark = pytest.mark.integration

ACTIVE_OWNER = "a" * 64
STALE_OWNER = "b" * 64
TERMINAL_OWNER = "c" * 64


def _create_task(
    runtime: Any,
    *,
    kind: str = "content_render",
    status: str = "running",
    owner: str | None = ACTIVE_OWNER,
    lease_expires_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> tuple[int, int]:
    idempotency_key = "orphan-contract-" + uuid.uuid4().hex
    if status == "running" and lease_expires_at is None:
        lease_expires_at = datetime.now(UTC) + timedelta(hours=1)
    phase = {
        "queued": "queued",
        "running": "rendering",
        "succeeded": "published",
        "failed": "failed",
        "cancelled": "cancelled",
    }[status]

    async def create() -> tuple[int, int]:
        async with runtime.worker.generation.sessions() as session, session.begin():
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
                title="Generation orphan contract",
                body_markdown="# Frozen orphan test input\n\nNo model is called.",
                tables=[],
                source_refs=[],
            )
            task = await generation_service.create_render_task(
                session,
                identity,
                draft_id=int(draft.id),
                expected_revision=int(draft.revision),
                file_format="md",
                file_name=idempotency_key + ".md",
                idempotency_key=idempotency_key,
            )
            # Export keys are covered by the same durable task row contract;
            # these sweeper-only tasks do not dispatch an export plan.
            task.kind = kind
            task.status, task.phase = status, phase
            task.execution_owner = owner
            task.execution_lease_expires_at = lease_expires_at
            if updated_at is not None:
                task.updated_at = updated_at
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


def _put(root_storage: Any, bucket: str, key: str, payload: bytes = b"orphan") -> None:
    root_storage.put_object(
        bucket,
        key,
        io.BytesIO(payload),
        len(payload),
        content_type="text/plain",
    )


def _assert_absent(root_storage: Any, bucket: str, key: str) -> None:
    with pytest.raises(S3Error) as error:
        root_storage.stat_object(bucket, key)
    assert error.value.code == "NoSuchKey"


def _assert_present(root_storage: Any, bucket: str, key: str) -> None:
    assert root_storage.stat_object(bucket, key).size > 0


def test_generated_and_export_scans_keep_registered_active_and_out_of_scope_objects(
    runtime, database, ingestion_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    assert storage is not None
    generated_task, _ = _create_task(runtime, kind="content_render")
    export_task, _ = _create_task(runtime, kind="database_export")
    tasks = {"generated": generated_task, "export": export_task}
    storage_statuses = (
        "staged",
        "available",
        "pending_delete",
        "deleting",
        "deleted",
        "error",
    )
    registered: list[StorageObject] = []
    registered_keys: list[str] = []
    active_keys: list[str] = []
    for index, status in enumerate(storage_statuses):
        prefix = "generated" if index % 2 == 0 else "export"
        key = storage.new_key(prefix, task_id=tasks[prefix], owner_token=STALE_OWNER)
        payload = f"registered-{status}".encode()
        _put(root_storage, settings.minio_bucket, key, payload)
        registered_keys.append(key)
        registered.append(
            StorageObject(
                bucket_name=settings.minio_bucket,
                object_key=key,
                object_type=prefix,
                size_bytes=len(payload),
                content_type="text/plain",
                content_sha256=hashlib.sha256(payload).hexdigest(),
                status=status,
            )
        )
    for prefix in ("generated", "export"):
        key = storage.new_key(prefix, task_id=tasks[prefix], owner_token=ACTIVE_OWNER)
        _put(root_storage, settings.minio_bucket, key)
        active_keys.append(key)

    # Exact-shape but unowned keys and older/foreign shapes must stay available
    # for manual review; the foreign bucket is outside this storage instance.
    unknown_task_key = (
        f"generated/20200101T000000Z/task-9223372036854775807/attempt-{'d' * 64}/{uuid.uuid4().hex}"
    )
    legacy_key = f"generated/20200101T000000Z/{uuid.uuid4().hex}"
    invalid_date_key = (
        f"generated/20260230T000000Z/task-{generated_task}/attempt-{'e' * 64}/{uuid.uuid4().hex}"
    )
    mismatched_prefix_key = storage.new_key(
        "export", task_id=generated_task, owner_token=STALE_OWNER
    )
    outside_key = "outside/orphan/" + uuid.uuid4().hex
    scoped_keys = [
        unknown_task_key,
        legacy_key,
        invalid_date_key,
        mismatched_prefix_key,
        outside_key,
    ]
    for key in scoped_keys:
        _put(root_storage, settings.minio_bucket, key)

    real_list = storage.client.list_objects

    def list_with_old_metadata(bucket: str, *, prefix: str, recursive: bool, **kwargs: Any) -> Any:
        old = datetime.now(UTC) - timedelta(days=2)
        for item in real_list(bucket, prefix=prefix, recursive=recursive, **kwargs):
            yield SimpleNamespace(object_name=item.object_name, last_modified=old)

    monkeypatch.setattr(storage.client, "list_objects", list_with_old_metadata)

    foreign_bucket = "em-orphan-foreign-" + uuid.uuid4().hex
    foreign_key = storage.new_key("generated", task_id=generated_task, owner_token=STALE_OWNER)
    root_storage.make_bucket(foreign_bucket)
    try:
        _put(root_storage, foreign_bucket, foreign_key)

        async def register() -> None:
            async with runtime.worker.generation.sessions() as session, session.begin():
                session.add_all(registered)

        runtime.run(register)
        removed = runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0)

        assert removed == 0
        for key in registered_keys + active_keys + scoped_keys:
            _assert_present(root_storage, settings.minio_bucket, key)
        _assert_present(root_storage, foreign_bucket, foreign_key)
        with Session(database.engine) as session:
            assert set(
                session.scalars(
                    select(StorageObject.object_key).where(
                        StorageObject.object_key.in_(registered_keys)
                    )
                )
            ) == set(registered_keys)
    finally:
        root_storage.remove_object(foreign_bucket, foreign_key)
        root_storage.remove_bucket(foreign_bucket)


def test_embedded_creation_time_and_minio_mtime_must_both_pass_default_grace(
    runtime, ingestion_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    assert storage is not None
    task_id, _ = _create_task(runtime, status="failed", owner=TERMINAL_OWNER)
    future_time = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    future_key = (
        f"generated/{future_time}/task-{task_id}/attempt-{TERMINAL_OWNER}/{uuid.uuid4().hex}"
    )
    old_key = (
        f"generated/20200101T000000Z/task-{task_id}/attempt-{TERMINAL_OWNER}/{uuid.uuid4().hex}"
    )
    _put(root_storage, settings.minio_bucket, future_key)
    _put(root_storage, settings.minio_bucket, old_key)

    real_list = storage.client.list_objects
    real_stat = storage.client.stat_object
    stat_calls: dict[str, int] = {}
    old_mtime = datetime.now(UTC) - timedelta(days=2)

    def list_with_old_metadata(bucket: str, *, prefix: str, recursive: bool, **kwargs: Any) -> Any:
        for item in real_list(bucket, prefix=prefix, recursive=recursive, **kwargs):
            if item.object_name in {future_key, old_key}:
                yield SimpleNamespace(object_name=item.object_name, last_modified=old_mtime)
            else:
                yield item

    def count_real_stat(bucket: str, object_name: str, **kwargs: Any) -> Any:
        stat_calls[object_name] = stat_calls.get(object_name, 0) + 1
        return real_stat(bucket, object_name, **kwargs)

    monkeypatch.setattr(storage.client, "list_objects", list_with_old_metadata)
    monkeypatch.setattr(storage.client, "stat_object", count_real_stat)
    # 86400 is the runtime default. The valid future path time rejects that
    # object despite its controlled old listing mtime. The 2020 path time passes,
    # but its current real MinIO mtime keeps the second object at the second stat.
    removed = runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 86400)
    assert removed == 0
    assert future_key not in stat_calls
    assert stat_calls.get(old_key) == 1
    _assert_present(root_storage, settings.minio_bucket, future_key)
    _assert_present(root_storage, settings.minio_bucket, old_key)


def test_database_lookup_failure_leaves_candidate_and_publisher_row_lock_serializes_gc(
    runtime, database, ingestion_settings
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    assert storage is not None

    failed_task, _ = _create_task(runtime, status="failed", owner=TERMINAL_OWNER)
    failed_key = storage.new_key("generated", task_id=failed_task, owner_token=TERMINAL_OWNER)
    _put(root_storage, settings.minio_bucket, failed_key)
    async_engine = runtime.worker.generation.sessions.kw["bind"]
    sync_engine = async_engine.sync_engine

    def fail_task_lookup(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().replace('"', "").split())
        if "from agent.generation_tasks" in normalized and "for update skip locked" in normalized:
            raise RuntimeError("injected generation task lookup failure")

    event.listen(sync_engine, "before_cursor_execute", fail_task_lookup)
    try:
        with pytest.raises(RuntimeError, match="injected generation task lookup failure"):
            runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0)
    finally:
        event.remove(sync_engine, "before_cursor_execute", fail_task_lookup)
    _assert_present(root_storage, settings.minio_bucket, failed_key)
    root_storage.remove_object(settings.minio_bucket, failed_key)

    task_id, _ = _create_task(runtime, owner=ACTIVE_OWNER)
    publishing_key = storage.new_key("generated", task_id=task_id, owner_token=STALE_OWNER)
    payload = b"object registered by the publication transaction"
    _put(root_storage, settings.minio_bucket, publishing_key, payload)
    with Session(database.engine) as publisher:
        with publisher.begin():
            task = publisher.scalar(
                select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
            )
            assert task is not None
            publisher.add(
                StorageObject(
                    bucket_name=settings.minio_bucket,
                    object_key=publishing_key,
                    object_type="generated",
                    size_bytes=len(payload),
                    content_type="text/plain",
                    content_sha256=hashlib.sha256(payload).hexdigest(),
                    status="available",
                )
            )
            publisher.flush()
            # A concurrent collector uses SKIP LOCKED and leaves the in-flight
            # publication alone before its registry row commits.
            assert runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0) == 0
            _assert_present(root_storage, settings.minio_bucket, publishing_key)

    # Once publication commits, the durable registry independently protects it.
    assert runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0) == 0
    _assert_present(root_storage, settings.minio_bucket, publishing_key)


def test_expired_owner_is_fenced_stale_owner_is_untouched_and_terminal_owner_is_preserved(
    runtime, database, ingestion_settings
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    assert storage is not None
    old_update = datetime(2000, 1, 1, tzinfo=UTC)
    expired_id, _ = _create_task(
        runtime,
        kind="database_export",
        owner=ACTIVE_OWNER,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=2),
        updated_at=old_update,
    )
    stale_id, _ = _create_task(
        runtime,
        kind="content_render",
        owner=STALE_OWNER,
        lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    terminal_id, _ = _create_task(
        runtime,
        kind="content_render",
        status="failed",
        owner=TERMINAL_OWNER,
    )
    queued_id, _ = _create_task(runtime, kind="content_render", status="queued", owner=None)

    expired_key = storage.new_key("export", task_id=expired_id, owner_token=ACTIVE_OWNER)
    stale_key = storage.new_key("generated", task_id=stale_id, owner_token=ACTIVE_OWNER)
    terminal_key = storage.new_key("generated", task_id=terminal_id, owner_token=TERMINAL_OWNER)
    queued_key = storage.new_key("generated", task_id=queued_id, owner_token=STALE_OWNER)
    for key in (expired_key, stale_key, terminal_key, queued_key):
        _put(root_storage, settings.minio_bucket, key)

    with Session(database.engine) as session:
        stale_before = session.get(GenerationTask, stale_id)
        terminal_before = session.get(GenerationTask, terminal_id)
        assert stale_before is not None and terminal_before is not None
        stale_snapshot = (
            stale_before.execution_owner,
            stale_before.execution_lease_expires_at,
            stale_before.updated_at,
        )
        terminal_owner = terminal_before.execution_owner
        assert terminal_owner == TERMINAL_OWNER

    removed = runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0)
    assert removed == 3
    _assert_absent(root_storage, settings.minio_bucket, expired_key)
    _assert_absent(root_storage, settings.minio_bucket, stale_key)
    _assert_absent(root_storage, settings.minio_bucket, terminal_key)
    _assert_present(root_storage, settings.minio_bucket, queued_key)

    with Session(database.engine) as session:
        expired = session.get(GenerationTask, expired_id)
        stale = session.get(GenerationTask, stale_id)
        terminal = session.get(GenerationTask, terminal_id)
        assert expired is not None and stale is not None and terminal is not None
        assert expired.status == "running"
        assert expired.execution_owner is None and expired.execution_lease_expires_at is None
        assert expired.updated_at > old_update
        assert (
            stale.execution_owner,
            stale.execution_lease_expires_at,
            stale.updated_at,
        ) == stale_snapshot
        assert terminal.execution_owner == terminal_owner

    assert (
        runtime.run(runtime.worker.generation._renew_execution_lease, expired_id, ACTIVE_OWNER)
        is False
    )
    with Session(database.engine) as session:
        expired = session.get(GenerationTask, expired_id)
        assert expired is not None
        assert expired.execution_owner is None and expired.execution_lease_expires_at is None


def test_delete_failure_retries_and_a_late_put_is_collected_on_the_next_sweep(
    runtime, ingestion_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    assert storage is not None
    task_id, _ = _create_task(runtime, status="failed", kind="content_render", owner=TERMINAL_OWNER)
    failed_key = storage.new_key("generated", task_id=task_id, owner_token=TERMINAL_OWNER)
    late_key = storage.new_key("generated", task_id=task_id, owner_token=TERMINAL_OWNER)
    refreshed_key = storage.new_key("generated", task_id=task_id, owner_token=TERMINAL_OWNER)
    _put(root_storage, settings.minio_bucket, failed_key)
    _put(root_storage, settings.minio_bucket, late_key)
    _put(root_storage, settings.minio_bucket, refreshed_key)

    real_remove = storage.remove
    real_stat = storage.client.stat_object
    failed_once = False
    late_put_done = False
    refreshed_stat_once = False

    class ControlledClock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime.now(tz)
            return current + (timedelta(seconds=2) if late_put_done else timedelta(0))

    monkeypatch.setattr(dispatch_module, "datetime", ControlledClock)

    def fail_then_remove(key: str) -> None:
        nonlocal failed_once, late_put_done
        if key == failed_key and not failed_once:
            failed_once = True
            raise OSError("injected MinIO delete failure")
        real_remove(key)
        if key == late_key:
            # Model a PUT completing just after this sweep's delete returns.
            _put(root_storage, settings.minio_bucket, late_key, b"late upload")
            late_put_done = True

    def stat_with_refresh(bucket: str, object_name: str, **kwargs: Any) -> Any:
        nonlocal refreshed_stat_once
        metadata = real_stat(bucket, object_name, **kwargs)
        if object_name == refreshed_key and not refreshed_stat_once:
            refreshed_stat_once = True
            return SimpleNamespace(last_modified=datetime.now(UTC) + timedelta(days=1))
        return metadata

    monkeypatch.setattr(storage, "remove", fail_then_remove)
    monkeypatch.setattr(storage.client, "stat_object", stat_with_refresh)
    first_removed = runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0)
    assert failed_once
    assert refreshed_stat_once
    assert first_removed == 1
    _assert_present(root_storage, settings.minio_bucket, failed_key)
    _assert_present(root_storage, settings.minio_bucket, late_key)
    _assert_present(root_storage, settings.minio_bucket, refreshed_key)

    monkeypatch.setattr(storage, "remove", real_remove)
    monkeypatch.setattr(storage.client, "stat_object", real_stat)
    second_removed = runtime.run(sweep_orphans, runtime.worker.generation.sessions, storage, 0)
    assert second_removed == 3
    _assert_absent(root_storage, settings.minio_bucket, failed_key)
    _assert_absent(root_storage, settings.minio_bucket, late_key)
    _assert_absent(root_storage, settings.minio_bucket, refreshed_key)


def test_sweeper_does_not_delete_an_object_being_published(
    runtime, database, ingestion_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root_storage, _ = ingestion_settings
    storage = runtime.worker.generation.storage
    worker = runtime.worker.generation
    assert storage is not None
    task_id, event_id = _create_task(runtime, status="queued", owner=None)
    keys: list[str] = []
    original_new_key = storage.new_key

    def track_key(prefix: str = "original", **kwargs: Any) -> str:
        key = original_new_key(prefix, **kwargs)
        keys.append(key)
        return key

    monkeypatch.setattr(storage, "new_key", track_key)
    original_list = storage.client.list_objects

    def list_with_old_mtime(bucket: str, *, prefix: str, recursive: bool, **kwargs: Any) -> Any:
        for item in original_list(bucket, prefix=prefix, recursive=recursive, **kwargs):
            if item.object_name in keys:
                yield SimpleNamespace(
                    object_name=item.object_name,
                    last_modified=datetime.now(UTC) - timedelta(days=2),
                )
            else:
                yield item

    monkeypatch.setattr(storage.client, "list_objects", list_with_old_mtime)
    uploaded = threading.Event()
    release_verify = threading.Event()
    real_verify = storage.verify

    def verify_then_wait(key: str, size: int, digest: str) -> None:
        real_verify(key, size, digest)
        uploaded.set()
        if not release_verify.wait(timeout=30):
            raise TimeoutError("the orphan sweep race did not release verification")

    monkeypatch.setattr(storage, "verify", verify_then_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.run, worker.handle_event, event_id)
        try:
            assert uploaded.wait(timeout=30)
            assert len(keys) == 1
            assert runtime.run(sweep_orphans, worker.sessions, storage, 0) == 0
            _assert_present(root_storage, settings.minio_bucket, keys[0])
        finally:
            release_verify.set()
        assert future.result(timeout=30) is True

    with Session(database.engine) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None and task.status == "succeeded"
        assert task.file_id is not None
        obj = session.get(StorageObject, task.file_id)
        assert obj is not None and obj.object_key == keys[0]
        generated = session.scalars(
            select(GeneratedFile).where(GeneratedFile.task_id == task_id)
        ).one()
        assert generated.storage_object_id == task.file_id
